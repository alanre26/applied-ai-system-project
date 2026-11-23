"""Tests for the PawPal+ planning agent. Claude is replaced with a scripted fake so these run offline."""

import json
from datetime import date, time
from types import SimpleNamespace

import pytest

from agent import PlanningAgent, accept_changes
from pawpal_system import Owner, Pet, Task
from rag import Retriever, load_notes

TODAY = date(2026, 10, 25)


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    return Retriever(load_notes())


def reply(*changes, summary="Moved things around so nothing overlaps."):
    """Build the JSON Claude would return."""
    return json.dumps({
        "summary": summary,
        "changes": [{"task_id": c[0], "action": c[1], "value": c[2], "reason": "test"} for c in changes],
    })


class ScriptedClient:
    """Fake Claude that returns the given replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


class BrokenClient:
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        raise RuntimeError("network down")


def make_owner():
    """Mochi (dog) and Luna (cat). Dinner and litter box both at 18:00, so the owner is double-booked."""
    owner = Owner("Alan", available_minutes=120)
    mochi, luna = Pet("Mochi", "dog", 3), Pet("Luna", "cat", 5)
    owner.add_pet(mochi)
    owner.add_pet(luna)
    tasks = {
        "walk": (mochi, Task(0, "Morning walk", 30, "high", preferred_time=time(7, 30))),
        "meds": (mochi, Task(0, "Medication", 5, "low", preferred_time=time(8, 0))),
        "dinner": (mochi, Task(0, "Dinner", 10, "high", preferred_time=time(18, 0))),
        "litter": (luna, Task(0, "Litter box", 5, "medium", preferred_time=time(18, 0))),
    }
    for pet, task in tasks.values():
        task.task_id = owner.new_task_id()
        pet.add_task(task)
    return owner, {name: task for name, (_, task) in tasks.items()}


def test_agent_fixes_time_conflict(retriever):
    owner, t = make_owner()
    client = ScriptedClient(reply((t["litter"].task_id, "set_time", "18:15")))
    result = PlanningAgent(owner, retriever, client).run("", TODAY)

    assert result.used_ai and result.attempts == 1
    assert result.problems == []
    assert len(result.accepted) == 1
    assert result.scheduler.detect_conflicts(result.plan) == []


def test_prompt_includes_request_conflicts_and_must_do(retriever):
    owner, t = make_owner()
    client = ScriptedClient(reply())
    PlanningAgent(owner, retriever, client).run("Vet visit from 2 to 4pm", TODAY)

    prompt = client.requests[0]["messages"][0]["content"]
    assert "Vet visit from 2 to 4pm" in prompt
    assert "Owner is double-booked" in prompt                       # the conflict was shown to Claude
    assert f"task_id {t['meds'].task_id}: Medication" in prompt and "MUST-DO" in prompt
    assert 'source="medication.md"' in prompt                       # retrieved notes were included
    assert client.requests[0]["output_config"]["format"]["type"] == "json_schema"


def test_guardrail_blocks_skipping_medication_then_retry_fixes_it(retriever):
    owner, t = make_owner()
    client = ScriptedClient(
        reply((t["meds"].task_id, "skip_today", ""), (t["litter"].task_id, "set_time", "18:15")),
        reply((t["litter"].task_id, "set_time", "18:15")),
    )
    result = PlanningAgent(owner, retriever, client).run("", TODAY)

    assert result.attempts == 2
    assert result.problems == []
    assert t["meds"].task_id in [task.task_id for task in result.plan]
    retry_message = client.requests[1]["messages"][-1]["content"]
    assert "must-do" in retry_message


def test_guardrail_rejects_bad_task_id_time_and_priority(retriever):
    owner, t = make_owner()
    bad = reply((999, "set_time", "09:00"), (t["walk"].task_id, "set_time", "25:99"),
                (t["meds"].task_id, "set_priority", "low"), (t["litter"].task_id, "set_priority", "urgent"))
    client = ScriptedClient(bad, bad)
    result = PlanningAgent(owner, retriever, client).run("", TODAY)

    reasons = [c.rejected_because for c in result.rejected]
    assert len(result.rejected) == 4 and result.accepted == []
    assert any("not one of today's tasks" in r for r in reasons)
    assert any("isn't a valid time" in r for r in reasons)
    assert any("care note" in r for r in reasons)
    assert any("isn't a valid priority" in r for r in reasons)
    assert result.problems                                       # still reported after the retry


def test_agent_falls_back_when_claude_is_down(retriever):
    owner, _ = make_owner()
    result = PlanningAgent(owner, retriever, BrokenClient()).run("", TODAY)

    assert not result.used_ai
    assert len(result.plan) == 4                                 # the rule-based plan still works
    assert any("Time conflict" in p for p in result.problems)    # and the conflict is still reported


def test_agent_falls_back_on_invalid_json(retriever):
    owner, _ = make_owner()
    result = PlanningAgent(owner, retriever, ScriptedClient("not json")).run("", TODAY)
    assert not result.used_ai
    assert any("valid JSON" in s for s in result.steps)


def test_agent_never_changes_owner_data(retriever):
    owner, t = make_owner()
    client = ScriptedClient(reply((t["litter"].task_id, "set_time", "18:15"), (t["walk"].task_id, "skip_today", "")))
    PlanningAgent(owner, retriever, client).run("", TODAY)

    assert t["litter"].preferred_time == time(18, 0)
    assert t["meds"].priority == "low"
    assert len(owner.get_all_tasks()) == 4


def test_accept_changes_saves_time_but_not_skips(retriever):
    owner, t = make_owner()
    client = ScriptedClient(reply((t["litter"].task_id, "set_time", "18:15"), (t["walk"].task_id, "skip_today", "")))
    result = PlanningAgent(owner, retriever, client).run("", TODAY)

    saved = accept_changes(owner, result.accepted)
    assert saved == 1
    assert t["litter"].preferred_time == time(18, 15)
    assert t["walk"] in owner.get_pet("Mochi").tasks


def test_no_tasks_means_no_claude_call(retriever):
    owner = Owner("Alan", 60)
    owner.add_pet(Pet("Mochi", "dog"))
    client = ScriptedClient()
    result = PlanningAgent(owner, retriever, client).run("", TODAY)
    assert client.requests == []
    assert result.plan == []

"""Reliability evaluation for PawPal+.

Runs a fixed set of cases and reports pass/fail for each one:
- Retrieval: does the right care note come back for a task or question?
- Guardrails: are bad inputs and bad AI suggestions handled safely? (uses a scripted fake Claude)
- Live AI: do real Claude answers and agent plans meet the criteria? (needs ANTHROPIC_API_KEY,
  skipped otherwise; costs roughly $0.30 per run)

Results are printed and saved to evidence/evaluation_results.md as a markdown table.
Run with:  python evaluate.py
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from types import SimpleNamespace

from agent import PlanningAgent
from demo import TODAY, sample_owner
from pawpal_system import Owner, Pet, Task
from rag import Retriever, ask_pawpal, load_notes

OUTPUT = Path(__file__).parent / "evidence" / "evaluation_results.md"
LIVE = bool(os.environ.get("ANTHROPIC_API_KEY"))
AGENT_REPEATS = 3   # how many times the consistency case reruns the same agent request


@dataclass
class Result:
    category: str
    test_input: str
    criteria: str
    passed: bool | None      # None = skipped
    notes: str = ""


results: list[Result] = []
ai_outputs: list[tuple[str, str]] = []   # (input, output) saved for human review


def record(category, test_input, criteria, passed, notes=""):
    results.append(Result(category, test_input, criteria, passed, notes))
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[passed]
    print(f"  [{mark}] {category}: {test_input} -> {notes or criteria}")


# --- Fake Claude clients for the guardrail cases -------------------------------

class ScriptedClient:
    """Returns the given replies in order and counts the requests."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


class DownClient:
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._fail))

    def _fail(self, **kwargs):
        raise ConnectionError("simulated outage")


def changes(*items):
    return json.dumps({"summary": "test", "changes": [
        {"task_id": i, "action": a, "value": v, "reason": "test"} for i, a, v in items]})


def task_id(owner: Owner, title: str) -> int:
    return next(t.task_id for t in owner.get_all_tasks() if t.title == title)


# --- Cases ------------------------------------------------------------------------

def retrieval_cases(retriever: Retriever) -> None:
    print("\nRetrieval")
    for query, species, expected in [
        ("Medication", "dog", "medication.md"),
        ("Morning walk", "dog", "dog_exercise.md"),
        ("Dinner", "cat", "feeding_cats.md"),
        ("Litter box", "cat", "litter_box.md"),
        ("Litter box", "dog", None),                     # a dog should never get the litter box note
        ("My dog ate chocolate", "dog", "vet_warning_signs.md"),
        ("Can I give my dog ibuprofen?", "dog", "medication.md"),
        ("Who won the World Series this year?", "dog", None),
    ]:
        found = retriever.search(query, species=species, k=1)
        top = found[0].note.source if found else None
        record("Retrieval", f'"{query}" ({species})', f"top note is {expected or 'nothing'}",
               top == expected, f"got {top or 'nothing'}")


def guardrail_cases(retriever: Retriever) -> None:
    print("\nGuardrails")
    owner = sample_owner()

    client = ScriptedClient()
    a = ask_pawpal("Who won the World Series this year?", owner, retriever, client=client)
    record("Guardrail", "Off-topic question", "Claude is not called, owner told to ask elsewhere",
           client.calls == 0 and not a.used_ai, f"Claude calls: {client.calls}")

    a = ask_pawpal("   ", owner, retriever, client=ScriptedClient())
    record("Guardrail", "Empty question", "Handled without an error", not a.used_ai, a.text)

    meds, walk, litter = task_id(owner, "Medication"), task_id(owner, "Morning walk"), task_id(owner, "Litter box")
    fixed = changes((litter, "set_time", "18:15"))
    r = PlanningAgent(owner, retriever, ScriptedClient(changes((meds, "skip_today", ""), (litter, "set_time", "18:15")), fixed)).run("", TODAY)
    # The final result only holds the last try, so look for the rejection in the step trace.
    trace = "\n".join(r.steps)
    record("Guardrail", "Agent tries to skip medication", "Change rejected, meds still planned, retry fixes plan",
           "must-do task" in trace and meds in [t.task_id for t in r.plan] and not r.problems,
           f"rejected on try 1 (must-do), fixed on try {r.attempts}")

    r = PlanningAgent(owner, retriever, ScriptedClient(changes((walk, "set_time", "25:99"), (999, "set_time", "09:00")), fixed)).run("", TODAY)
    trace = "\n".join(r.steps)
    record("Guardrail", "Agent suggests invalid time and unknown task", "Both rejected",
           "isn't a valid time" in trace and "not one of today's tasks" in trace,
           "rejected: invalid time, unknown task")

    r = PlanningAgent(owner, retriever, DownClient()).run("", TODAY)
    record("Guardrail", "Claude unreachable", "Falls back to rule-based plan, conflict still reported",
           not r.used_ai and len(r.plan) > 0 and any("conflict" in p.lower() for p in r.problems),
           f"plan has {len(r.plan)} tasks, used_ai={r.used_ai}")

    r = PlanningAgent(owner, retriever, ScriptedClient("this is not json")).run("", TODAY)
    record("Guardrail", "Claude returns invalid JSON", "Falls back without crashing",
           not r.used_ai and len(r.plan) > 0, r.steps[-1])

    before = [(t.priority, t.preferred_time) for t in owner.get_all_tasks()]
    PlanningAgent(owner, retriever, ScriptedClient(fixed)).run("", TODAY)
    record("Guardrail", "Agent run before owner accepts", "Owner's saved tasks are unchanged",
           before == [(t.priority, t.preferred_time) for t in owner.get_all_tasks()])


def _overlaps(task: Task, start: time, end: time) -> bool:
    if task.preferred_time is None:
        return False
    s = task.preferred_time.hour * 60 + task.preferred_time.minute
    return s < end.hour * 60 + end.minute and s + task.duration_minutes > start.hour * 60 + start.minute


def live_cases(retriever: Retriever) -> None:
    print("\nLive AI" + ("" if LIVE else " (skipped: no ANTHROPIC_API_KEY)"))
    live = [
        ("Ask", "Missed medication dose", "Uses Claude, cites [medication.md], under 150 words"),
        ("Ask", "Dog ate chocolate", "Uses Claude, tells owner to contact a vet"),
        ("Ask", "Best dog food brand (not in notes)", "Doesn't name a brand, says notes don't cover it or to ask a vet"),
        ("Agent", "Dinner and litter box both at 18:00", "No conflicts left, all must-do tasks planned"),
        ("Agent", "\"I'm busy from 7 to 9am\"", "No planned task overlaps 07:00-09:00, meds still planned"),
        ("Agent", f"Same request run {AGENT_REPEATS} times", "Every run ends with no problems (consistency)"),
    ]
    if not LIVE:
        for category, test_input, criteria in live:
            record(f"Live {category}", test_input, criteria, None, "skipped")
        return

    owner = sample_owner()

    a = ask_pawpal("What should I do if I miss a medication dose for Mochi?", owner, retriever)
    ai_outputs.append(("What should I do if I miss a medication dose for Mochi?", a.text))
    record("Live Ask", live[0][1], live[0][2],
           a.used_ai and "[medication.md]" in a.text and len(a.text.split()) < 150, f"{len(a.text.split())} words")

    a = ask_pawpal("Mochi ate a chocolate bar. What should I do?", owner, retriever)
    ai_outputs.append(("Mochi ate a chocolate bar. What should I do?", a.text))
    record("Live Ask", live[1][1], live[1][2], a.used_ai and "vet" in a.text.lower(), f"sources: {a.sources}")

    a = ask_pawpal("What brand of dog food is the best?", owner, retriever)
    ai_outputs.append(("What brand of dog food is the best?", a.text))
    brands = ["purina", "hill", "royal canin", "blue buffalo", "pedigree", "iams", "orijen"]
    record("Live Ask", live[2][1], live[2][2],
           a.used_ai and not any(b in a.text.lower() for b in brands)
           and ("vet" in a.text.lower() or "don't" in a.text.lower() or "do not" in a.text.lower()),
           f"sources: {a.sources}")

    r = PlanningAgent(sample_owner(), retriever).run("", TODAY)
    ai_outputs.append(("Agent: (no request), dinner and litter box overlap", r.summary))
    record("Live Agent", live[3][1], live[3][2], r.used_ai and not r.problems,
           f"attempts: {r.attempts}, accepted: {[c.describe() for c in r.accepted]}")

    owner = sample_owner()
    r = PlanningAgent(owner, retriever).run("I'm busy from 7 to 9am today.", TODAY)
    ai_outputs.append(("Agent: I'm busy from 7 to 9am today.", r.summary))
    clash = [t.title for t in r.plan if _overlaps(t, time(7, 0), time(9, 0))]
    record("Live Agent", live[4][1], live[4][2],
           r.used_ai and not clash and task_id(owner, "Medication") in [t.task_id for t in r.plan],
           f"tasks still in 7-9am: {clash or 'none'}, attempts: {r.attempts}")

    runs = [PlanningAgent(sample_owner(), retriever).run("", TODAY) for _ in range(AGENT_REPEATS)]
    clean = sum(1 for r in runs if r.used_ai and not r.problems)
    record("Live Agent", live[5][1], live[5][2], clean == AGENT_REPEATS,
           f"{clean}/{AGENT_REPEATS} clean, attempts: {[r.attempts for r in runs]}")


# --- Report --------------------------------------------------------------------------

def save_report() -> None:
    ran = [r for r in results if r.passed is not None]
    passed = sum(1 for r in ran if r.passed)
    rows = "\n".join(
        f"| {i} | {r.category} | {r.test_input} | {r.criteria} | "
        f"{ {True: 'Pass', False: 'Fail', None: 'Skipped'}[r.passed] } | {r.notes.replace('|', '/')} |"
        for i, r in enumerate(results, 1)
    )
    outputs = "\n\n".join(f"**Input:** {q}\n\n> " + a.replace("\n", "\n> ") for q, a in ai_outputs)
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(
        f"# PawPal+ Evaluation Results\n\n"
        f"Run date: {TODAY:%B %d, %Y} · Live Claude cases: {'run' if LIVE else 'skipped (no API key)'}\n\n"
        f"**{passed} of {len(ran)} cases passed.**\n\n"
        f"| # | Category | Input | Criteria | Result | Notes |\n|---|---|---|---|---|---|\n{rows}\n"
        + (f"\n## AI outputs for human review\n\n{outputs}\n" if ai_outputs else ""),
        encoding="utf-8",
    )
    print(f"\n{passed} of {len(ran)} cases passed"
          + (f" ({len(results) - len(ran)} skipped)" if len(ran) < len(results) else ""))
    print(f"Saved to {OUTPUT.relative_to(Path(__file__).parent)}")


def main() -> None:
    logging.basicConfig(filename="pawpal.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    retriever = Retriever(load_notes())
    print(f"PawPal+ evaluation · live Claude cases: {'on' if LIVE else 'off (no ANTHROPIC_API_KEY)'}")
    retrieval_cases(retriever)
    guardrail_cases(retriever)
    live_cases(retriever)
    save_report()


if __name__ == "__main__":
    main()

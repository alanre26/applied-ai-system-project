"""Tests for the PawPal+ RAG layer. Claude is replaced with a fake client so these run offline."""

from datetime import date
from types import SimpleNamespace

import pytest

from pawpal_system import Owner, Pet, Scheduler, Task
from rag import Retriever, apply_care_notes, ask_pawpal, load_notes, plan_with_care_notes

TODAY = date(2026, 10, 25)


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    return Retriever(load_notes())


def make_owner(minutes: int = 120) -> Owner:
    """Owner with a dog (Mochi) and a cat (Luna), no tasks yet."""
    owner = Owner("Alan", available_minutes=minutes)
    owner.add_pet(Pet("Mochi", "dog", 3))
    owner.add_pet(Pet("Luna", "cat", 5))
    return owner


def add_task(owner: Owner, pet_name: str, title: str, minutes: int, priority: str) -> Task:
    task = Task(owner.new_task_id(), title, minutes, priority)
    owner.get_pet(pet_name).add_task(task)
    return task


class FakeClient:
    """Stands in for anthropic.Anthropic(). Records the request and returns a canned reply."""

    def __init__(self, reply: str = "Give it at the same time each day [medication.md].", stop_reason="end_turn"):
        self.requests = []
        self.reply = reply
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=self.reply)],
        )


class BrokenClient:
    """A client whose request always fails, like a network outage."""

    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        raise RuntimeError("network down")


# --- Retrieval ---------------------------------------------------------------

def test_all_notes_load():
    notes = load_notes()
    assert len(notes) >= 10
    assert all(n.tip for n in notes)


def test_search_finds_medication_note(retriever):
    results = retriever.search("Give Mochi her meds", species="dog")
    assert results[0].note.source == "medication.md"


def test_search_respects_species(retriever):
    """A dog should never get the cat litter box note."""
    dog_sources = [r.note.source for r in retriever.search("litter box", species="dog")]
    cat_sources = [r.note.source for r in retriever.search("litter box", species="cat")]
    assert "litter_box.md" not in dog_sources
    assert cat_sources[0] == "litter_box.md"


def test_search_with_no_useful_words_returns_nothing(retriever):
    assert retriever.search("what is the") == []


# --- Notes change the plan -----------------------------------------------------

def test_low_priority_medication_is_raised_to_high(retriever):
    owner = make_owner()
    meds = add_task(owner, "Mochi", "Medication", 5, "low")
    changes = apply_care_notes(owner, retriever)
    assert meds.priority == "high"
    assert any(c.raised and c.source == "medication.md" for c in changes)


def test_care_notes_never_lower_a_priority(retriever):
    owner = make_owner()
    brushing = add_task(owner, "Luna", "Brushing", 10, "high")  # grooming note says low
    apply_care_notes(owner, retriever)
    assert brushing.priority == "high"


def test_unmatched_task_is_left_alone(retriever):
    owner = make_owner()
    task = add_task(owner, "Mochi", "Vacuum couch", 10, "low")
    changes = apply_care_notes(owner, retriever)
    assert task.priority == "low"
    assert changes == []


def test_care_notes_change_which_task_gets_scheduled(retriever):
    """With 30 minutes, plain planning picks the walk; with notes, the low-priority meds get in first."""
    owner = make_owner(minutes=30)
    walk = add_task(owner, "Mochi", "Morning walk", 25, "medium")
    meds = add_task(owner, "Mochi", "Medication", 10, "low")

    plain = Scheduler(owner)
    plain.generate_plan(TODAY)
    assert meds in plain.skipped

    scheduler = Scheduler(owner)
    plan_with_care_notes(scheduler, retriever, TODAY)
    assert meds in scheduler.plan
    assert walk in scheduler.skipped
    assert "medication.md" in scheduler.reasons[meds.task_id]


def test_plan_with_care_notes_keeps_owner_priorities(retriever):
    """The raise only applies to the plan; the owner's saved task keeps what they entered."""
    owner = make_owner()
    meds = add_task(owner, "Mochi", "Medication", 5, "low")
    plan_with_care_notes(Scheduler(owner), retriever, TODAY)
    assert meds.priority == "low"


# --- Answering questions ---------------------------------------------------------

def test_ask_sends_retrieved_notes_to_claude(retriever):
    owner = make_owner()
    client = FakeClient()
    answer = ask_pawpal("What if I miss a medication dose?", owner, retriever, client=client)

    assert answer.used_ai
    assert "medication.md" in answer.sources
    prompt = client.requests[0]["messages"][0]["content"]
    assert 'source="medication.md"' in prompt        # the note text was sent
    assert "Mochi: dog" in prompt                    # and so were the owner's pets


def test_ask_off_topic_question_does_not_call_claude(retriever):
    client = FakeClient()
    answer = ask_pawpal("Who won the World Series?", make_owner(), retriever, client=client)
    assert not answer.used_ai
    assert client.requests == []


def test_ask_falls_back_to_notes_when_claude_fails(retriever):
    answer = ask_pawpal("How often should I scoop the litter box?", make_owner(), retriever, client=BrokenClient())
    assert not answer.used_ai
    assert "litter_box.md" in answer.text


def test_ask_falls_back_when_claude_refuses(retriever):
    client = FakeClient(reply="", stop_reason="refusal")
    answer = ask_pawpal("How much should I feed my dog?", make_owner(), retriever, client=client)
    assert not answer.used_ai
    assert answer.sources


def test_ask_empty_question(retriever):
    answer = ask_pawpal("   ", make_owner(), retriever, client=FakeClient())
    assert not answer.used_ai


def test_one_shared_everyday_word_is_not_a_match(retriever):
    """'this year' used to match the vet note ('once a year') and let an off-topic question reach Claude."""
    assert retriever.search("Who won the World Series this year?") == []
    client = FakeClient()
    answer = ask_pawpal("Who won the World Series this year?", make_owner(), retriever, client=client)
    assert not answer.used_ai and client.requests == []


def test_weak_matches_are_not_sent_as_context(retriever):
    results = retriever.search("What should I do if I forget to give Mochi his medication?", species="dog")
    assert [r.note.source for r in results] == ["medication.md"]

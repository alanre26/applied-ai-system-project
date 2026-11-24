"""End-to-end demo of PawPal+ for the README.

Runs three scenarios through the whole system and saves the output to evidence/demo_output.txt:
1. Care notes (RAG) changing which task makes the plan.
2. The AI agent planning a day around the owner's request.
3. Ask PawPal answering questions from the care notes, including one it should refuse.

Run with:  python demo.py
Without ANTHROPIC_API_KEY set, scenarios 2 and 3 show the fallback behavior instead of Claude.
"""

import logging
import os
from datetime import date, time
from pathlib import Path

from agent import PlanningAgent
from pawpal_system import Owner, Pet, Scheduler, Task
from rag import Retriever, ask_pawpal, load_notes, plan_with_care_notes

TODAY = date(2026, 11, 23)
OUTPUT = Path(__file__).parent / "evidence" / "demo_output.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    """Print a line and keep it so the whole run can be saved to a file."""
    print(text)
    lines.append(text)


def heading(title: str) -> None:
    say()
    say("=" * 70)
    say(f"  {title}")
    say("=" * 70)


def sample_owner(minutes: int = 60) -> Owner:
    """Alan with a dog (Mochi) and a cat (Luna). Dinner and litter box overlap at 18:00 on purpose."""
    owner = Owner("Alan", available_minutes=minutes)
    mochi, luna = Pet("Mochi", "dog", 3), Pet("Luna", "cat", 5)
    owner.add_pet(mochi)
    owner.add_pet(luna)
    for pet, title, minutes_, priority, at in [
        (luna, "Breakfast", 5, "high", time(7, 0)),
        (mochi, "Morning walk", 30, "high", time(7, 30)),
        (mochi, "Medication", 5, "low", time(8, 0)),
        (mochi, "Dinner", 10, "high", time(18, 0)),
        (luna, "Litter box", 5, "medium", time(18, 0)),
        (luna, "Play time", 15, "low", None),
    ]:
        pet.add_task(Task(owner.new_task_id(), title, minutes_, priority, preferred_time=at))
    return owner


def print_plan(scheduler: Scheduler, planned_priority: dict[int, str] | None = None) -> None:
    """Print the plan. planned_priority holds priorities a care note raised for this plan only."""
    planned_priority = planned_priority or {}
    used = sum(t.duration_minutes for t in scheduler.plan)
    say(f"  Plan ({used} of {scheduler.owner.available_minutes} min):")
    for t in scheduler.plan:
        when = f"{t.preferred_time:%H:%M}" if t.preferred_time else "Anytime"
        say(f"    {when:<8}{t.title:<14}{t.pet_name:<7}{t.duration_minutes:>3} min  {planned_priority.get(t.task_id, t.priority)}")
    for t in scheduler.skipped:
        say(f"    Not planned: {t.title} ({t.pet_name}) - {scheduler.reasons[t.task_id]}")
    for c in scheduler.detect_conflicts(scheduler.plan):
        say(f"    ! Conflict: {c}")


def scenario_care_notes(retriever: Retriever) -> None:
    heading("Scenario 1: care notes change the plan (RAG, no AI call)")
    owner = Owner("Alan", available_minutes=30)
    mochi = Pet("Mochi", "dog", 3)
    owner.add_pet(mochi)
    mochi.add_task(Task(owner.new_task_id(), "Morning walk", 25, "medium"))
    mochi.add_task(Task(owner.new_task_id(), "Medication", 10, "low"))
    say("Input: 30 minutes free. Morning walk (25 min, medium), Medication (10 min, low).")

    say("\n-- Basic scheduler (priority only) --")
    basic = Scheduler(owner)
    basic.generate_plan(TODAY)
    print_plan(basic)

    say("\n-- With care notes --")
    with_notes = Scheduler(owner)
    changes = plan_with_care_notes(with_notes, retriever, TODAY)
    for c in changes:
        if c.raised:
            say(f"  Retrieved {c.source}: '{c.tip}'")
            say(f"  -> {c.task_title} raised from {c.old_priority} to {c.new_priority}")
    print_plan(with_notes, {c.task_id: c.new_priority for c in changes})


def scenario_agent(retriever: Retriever) -> None:
    heading("Scenario 2: AI agent plans the day around a request")
    owner = sample_owner()
    request = "I'm out from 5:30 to 6:30pm today, and Luna's breakfast has to wait until after 7:30."
    say(f"Input request: \"{request}\"")
    say("Tasks: Breakfast 07:00 (Luna), Morning walk 07:30, Medication 08:00 (low), "
        "Dinner 18:00, Litter box 18:00 (Luna), Play time anytime. 60 min free.")

    result = PlanningAgent(owner, retriever).run(request, TODAY)
    say("\n-- Agent steps --")
    for step in result.steps:
        say(f"  {step}")
    say(f"\n-- Agent summary --\n  {result.summary}")
    if result.accepted:
        say("\n-- Accepted changes --")
        for c in result.accepted:
            say(f"  {c.describe()}: {c.reason}")
    if result.rejected:
        say("\n-- Rejected by guardrails --")
        for c in result.rejected:
            say(f"  {c.describe()}: {c.rejected_because}")
    say()
    print_plan(result.scheduler)
    say(f"\n  Problems left: {result.problems or 'none'}")
    saved = {t.title: f"{t.preferred_time:%H:%M}" for t in owner.get_all_tasks() if t.preferred_time}
    say(f"  Owner's saved tasks after the run: Dinner {saved['Dinner']}, Litter box {saved['Litter box']}, "
        f"Breakfast {saved['Breakfast']} (unchanged until the owner clicks Accept)")


def scenario_ask(retriever: Retriever) -> None:
    heading("Scenario 3: Ask PawPal (answers from the care notes)")
    owner = sample_owner()
    for question in [
        "What should I do if I forget to give Mochi his medication?",
        "Mochi just ate some chocolate. What should I do?",
        "Who won the World Series this year?",   # off-topic: guardrail should stop it
    ]:
        answer = ask_pawpal(question, owner, retriever)
        say(f"\nQ: {question}")
        say(f"Used Claude: {answer.used_ai} | Sources: {', '.join(answer.sources) or 'none'}")
        for line in answer.text.splitlines():
            say(f"A: {line}" if line == answer.text.splitlines()[0] else f"   {line}")


def main() -> None:
    logging.basicConfig(filename="pawpal.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    retriever = Retriever(load_notes())
    say(f"PawPal+ demo run - {TODAY:%A, %B %d, %Y}")
    say(f"Claude API key found: {'yes' if os.environ.get('ANTHROPIC_API_KEY') else 'no (showing fallback behavior)'}")
    say(f"Care notes loaded: {len(retriever.notes)}")

    scenario_care_notes(retriever)
    scenario_agent(retriever)
    scenario_ask(retriever)

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved to {OUTPUT.relative_to(Path(__file__).parent)}")


if __name__ == "__main__":
    main()

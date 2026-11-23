"""PawPal+ planning agent: plan, act, check, and retry once if the plan has problems.

How one run works:
1. Plan:  build a draft schedule with care notes applied, then ask Claude what to change
          for today based on the owner's request, the tasks, and the matching care notes.
2. Guard: check every change Claude suggests. Bad ones are rejected with a reason.
3. Act:   apply the good changes to a copy of the owner's data and run the normal Scheduler.
4. Check: look for time conflicts, must-do tasks that got cut, and rejected changes.
          If there are problems, tell Claude what went wrong and try once more.

The owner's real data is never changed here. The app shows the result and the owner
decides whether to accept the changes.
"""

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, time

from pawpal_system import PRIORITY_ORDER, Owner, Scheduler, Task
from rag import Retriever, apply_care_notes, call_claude

logger = logging.getLogger("pawpal.agent")

MAX_ATTEMPTS = 2   # first try plus one retry

SYSTEM_PROMPT = """You are the planning agent in PawPal+, a pet care scheduling app.
You get the owner's request for today, today's tasks, the current draft plan, and pet care notes.
Suggest the smallest set of changes that makes today's plan work for the owner. You can:
- set_priority: value is "low", "medium", or "high"
- set_time: value is a 24-hour time like "14:30"
- skip_today: value is "" (the task is left out today only)

Rules:
- Only change tasks listed under "Today's tasks", using their exact task_id.
- Fix every time conflict, usually by moving the lower-priority task to a free time.
- Tasks marked MUST-DO come from a care note. Never skip them or lower their priority.
- Respect times the owner says they're busy.
- If nothing needs to change, return an empty list of changes.
- Give each change a short reason, and a one or two sentence summary for the owner."""

CHANGES_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "action": {"type": "string", "enum": ["set_priority", "set_time", "skip_today"]},
                        "value": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["task_id", "action", "value", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "changes"],
        "additionalProperties": False,
    },
}


@dataclass
class Change:
    """One change Claude suggested, plus whether the guardrails accepted it."""

    task_id: int
    action: str
    value: str
    reason: str
    task_title: str = ""
    rejected_because: str | None = None

    def describe(self) -> str:
        what = {
            "set_priority": f"priority -> {self.value}",
            "set_time": f"time -> {self.value}",
            "skip_today": "skip today",
        }.get(self.action, f"{self.action} {self.value}")
        return f"{self.task_title or f'task {self.task_id}'}: {what}"


@dataclass
class AgentResult:
    """Everything the UI or demo needs to show what the agent did and why."""

    scheduler: Scheduler          # holds plan, skipped, and reasons (built on a copy of the data)
    summary: str
    accepted: list[Change] = field(default_factory=list)
    rejected: list[Change] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # anything still wrong after the last try
    steps: list[str] = field(default_factory=list)      # readable trace of each step
    used_ai: bool = False
    attempts: int = 0

    @property
    def plan(self) -> list[Task]:
        return self.scheduler.plan

    @property
    def skipped(self) -> list[Task]:
        return self.scheduler.skipped


def _parse_time(value: str) -> time | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return time(hour, minute) if hour < 24 and minute < 60 else None


class PlanningAgent:
    """Wraps the Scheduler with a Claude planning step, guardrails, and a self-check."""

    def __init__(self, owner: Owner, retriever: Retriever, client=None):
        self.owner = owner
        self.retriever = retriever
        self.client = client

    # --- Steps ----------------------------------------------------------------

    def _draft(self) -> tuple[Owner, dict[int, str]]:
        """Copy the owner's data and apply care notes. Returns the copy and each task's must-do floor."""
        draft = copy.deepcopy(self.owner)
        floors = {}
        for change in apply_care_notes(draft, self.retriever):
            note = next(n for n in self.retriever.notes if n.source == change.source)
            if note.min_priority:
                floors[change.task_id] = note.min_priority
        return draft, floors

    def _build_prompt(self, request: str, today: date, draft: Owner, floors: dict[int, str]) -> str:
        scheduler = Scheduler(draft)
        todays = scheduler.sort_by_time(scheduler.get_todays_tasks(today))
        baseline = scheduler.generate_plan(today)

        task_lines = []
        for t in todays:
            when = f"{t.preferred_time:%H:%M}" if t.preferred_time else "anytime"
            must = " MUST-DO" if floors.get(t.task_id) == "high" else ""
            task_lines.append(
                f"- task_id {t.task_id}: {t.title} ({t.pet_name}), {t.duration_minutes} min, "
                f"{t.priority} priority, at {when}{must}"
            )

        # Notes that match the owner's request (e.g. "it's hot" -> walking note), plus each task's note.
        species = {p.species for p in draft.pets} or {None}
        sources = {}
        for s in species:
            for r in self.retriever.search(request, species=s, k=2):
                sources[r.note.source] = r.note
        for t in todays:
            pet = draft.get_pet(t.pet_name)
            for r in self.retriever.search(t.title, species=pet.species, k=1):
                if r.keyword_hits:
                    sources[r.note.source] = r.note
        notes = "\n\n".join(f'<note source="{n.source}">\n{n.body}\n</note>' for n in sources.values())

        conflicts = scheduler.detect_conflicts(baseline) or ["none"]
        skipped = [f"{t.title} ({t.pet_name})" for t in scheduler.skipped] or ["none"]

        return (
            f"Owner's request for today: {request.strip() or '(no special request)'}\n"
            f"Time available today: {draft.available_minutes} minutes\n\n"
            f"Today's tasks:\n" + "\n".join(task_lines) + "\n\n"
            f"Current draft plan problems:\n"
            f"- Time conflicts: " + "; ".join(conflicts) + "\n"
            f"- Didn't fit: " + ", ".join(skipped) + "\n\n"
            f"Care notes:\n{notes or '(none)'}"
        )

    def _validate(self, raw: list[dict], draft: Owner, today: date, floors: dict[int, str]) -> tuple[list[Change], list[Change]]:
        """Guardrails: accept only changes that are safe and make sense for today's tasks."""
        todays = {t.task_id: t for t in Scheduler(draft).get_todays_tasks(today)}
        accepted, rejected = [], []
        for item in raw:
            change = Change(item.get("task_id", -1), item.get("action", ""), str(item.get("value", "")), item.get("reason", ""))
            task = todays.get(change.task_id)
            floor = floors.get(change.task_id)
            if task:
                change.task_title = f"{task.title} ({task.pet_name})"

            if task is None:
                change.rejected_because = "not one of today's tasks"
            elif change.action == "set_priority":
                if change.value not in PRIORITY_ORDER:
                    change.rejected_because = f"'{change.value}' isn't a valid priority"
                elif floor and PRIORITY_ORDER[change.value] < PRIORITY_ORDER[floor]:
                    change.rejected_because = f"a care note says this should stay at least {floor}"
            elif change.action == "set_time":
                if _parse_time(change.value) is None:
                    change.rejected_because = f"'{change.value}' isn't a valid time"
            elif change.action == "skip_today":
                if floor == "high":
                    change.rejected_because = "this is a must-do task according to a care note"
            else:
                change.rejected_because = f"unknown action '{change.action}'"

            (rejected if change.rejected_because else accepted).append(change)
        return accepted, rejected

    def _act(self, draft: Owner, changes: list[Change], today: date) -> Scheduler:
        """Apply accepted changes to a fresh copy of the draft and build the plan."""
        working = copy.deepcopy(draft)
        tasks = {t.task_id: t for t in working.get_all_tasks()}
        skip = []
        for c in changes:
            task = tasks[c.task_id]
            if c.action == "set_priority":
                task.priority = c.value
            elif c.action == "set_time":
                task.preferred_time = _parse_time(c.value)
            elif c.action == "skip_today":
                skip.append(task)

        # Skipped tasks are left out of planning, then listed with the agent's reason.
        for task in skip:
            working.get_pet(task.pet_name).tasks.remove(task)
        scheduler = Scheduler(working)
        scheduler.generate_plan(today)
        for task in skip:
            reason = next(c.reason for c in changes if c.task_id == task.task_id and c.action == "skip_today")
            scheduler.skipped.append(task)
            scheduler.reasons[task.task_id] = f"skipped today by the agent: {reason}"

        for c in changes:
            if c.task_id in scheduler.reasons and c.action != "skip_today":
                scheduler.reasons[c.task_id] += f". Agent: {c.reason}"
        return scheduler

    def _check(self, scheduler: Scheduler, rejected: list[Change], floors: dict[int, str]) -> list[str]:
        """Return a list of problems with the plan. Empty means it passed."""
        problems = [f"Time conflict: {c}" for c in scheduler.detect_conflicts(scheduler.plan)]
        problems += [
            f"Must-do task didn't make the plan: {t.title} ({t.pet_name})"
            for t in scheduler.skipped if floors.get(t.task_id) == "high"
        ]
        problems += [f"Rejected change: {c.describe()} ({c.rejected_because})" for c in rejected]
        return problems

    # --- Main loop --------------------------------------------------------------

    def run(self, request: str, today: date) -> AgentResult:
        """Plan today's schedule for the owner's request. Never raises; falls back to the rule-based plan."""
        draft, floors = self._draft()
        steps = [f"Retrieve: applied care notes to {len(self.owner.get_all_tasks())} tasks "
                 f"({sum(1 for f in floors.values() if f == 'high')} marked must-do)"]
        logger.info("Agent run started. Request: %r", request)

        fallback = Scheduler(draft)
        fallback.generate_plan(today)
        if not fallback.plan and not fallback.skipped:
            return AgentResult(fallback, "No tasks are due today.", steps=steps + ["Nothing to plan."])

        messages = [{"role": "user", "content": self._build_prompt(request, today, draft, floors)}]
        best = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text, problem = call_claude(messages, SYSTEM_PROMPT, client=self.client, output_format=CHANGES_SCHEMA)
            if problem:
                keeping = "keeping the previous try" if best else "using the rule-based plan with care notes"
                steps.append(f"Plan (try {attempt}): {problem}, so {keeping}")
                logger.warning("Agent falling back: %s", problem)
                break
            try:
                data = json.loads(text)
                raw_changes, summary = data["changes"], data["summary"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                steps.append(f"Plan (try {attempt}): Claude's reply wasn't valid JSON ({error})")
                logger.error("Agent got invalid JSON: %s", error)
                break

            accepted, rejected = self._validate(raw_changes, draft, today, floors)
            steps.append(f"Plan (try {attempt}): Claude suggested {len(raw_changes)} change(s)")
            steps.append(f"Guard: accepted {len(accepted)}, rejected {len(rejected)}"
                         + "".join(f"\n    - rejected {c.describe()}: {c.rejected_because}" for c in rejected))

            scheduler = self._act(draft, accepted, today)
            used = sum(t.duration_minutes for t in scheduler.plan)
            steps.append(f"Act: built plan with {len(scheduler.plan)} task(s), {used} of {draft.available_minutes} min")

            problems = self._check(scheduler, rejected, floors)
            result = AgentResult(scheduler, summary, accepted, rejected, problems, steps, used_ai=True, attempts=attempt)
            if best is None or len(problems) <= len(best.problems):
                best = result

            if not problems:
                steps.append("Check: passed (no conflicts, all must-do tasks planned)")
                break
            steps.append(f"Check: {len(problems)} problem(s)" + "".join(f"\n    - {p}" for p in problems))
            if attempt < MAX_ATTEMPTS:
                steps.append("Retry: sending the problems back to Claude")
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "Your changes left these problems:\n"
                        + "\n".join(f"- {p}" for p in problems)
                        + "\nReturn a corrected full list of changes."},
                ]

        if best is None:
            result = AgentResult(fallback, "Showing the standard plan with care notes applied.", steps=steps,
                                 problems=[f"Time conflict: {c}" for c in fallback.detect_conflicts(fallback.plan)])
            logger.info("Agent finished with fallback plan")
            return result

        best.steps = steps
        logger.info("Agent finished after %d attempt(s): %d accepted, %d rejected, %d problem(s) left",
                    best.attempts, len(best.accepted), len(best.rejected), len(best.problems))
        return best


def accept_changes(owner: Owner, changes: list[Change]) -> int:
    """Human review step: save the agent's time and priority changes to the owner's real tasks.

    skip_today changes aren't saved, since they only make sense for today. Returns how many were saved.
    """
    tasks = {t.task_id: t for t in owner.get_all_tasks()}
    saved = 0
    for c in changes:
        task = tasks.get(c.task_id)
        if task is None:
            continue
        if c.action == "set_priority":
            task.priority = c.value
            saved += 1
        elif c.action == "set_time":
            task.preferred_time = _parse_time(c.value)
            saved += 1
    logger.info("Owner accepted %d agent change(s)", saved)
    return saved

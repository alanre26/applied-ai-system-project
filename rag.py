"""PawPal+ RAG layer: looks up pet care notes and uses them to shape the plan and answer questions.

Two ways the retrieved notes change what the app does:
1. apply_care_notes() raises a task's priority when a note says that kind of task
   shouldn't be skipped (for example, medication), so the Scheduler plans it first.
2. ask_pawpal() sends the matching notes to Claude, and Claude answers the owner's
   question using only those notes, citing which file each fact came from.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pawpal_system import PRIORITY_ORDER, Owner, Scheduler

logger = logging.getLogger("pawpal.rag")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MODEL = "claude-opus-5-5"

# Common words that would match almost every note, so they're ignored when scoring.
STOP_WORDS = {
    "a", "an", "and", "are", "at", "be", "can", "do", "does", "for", "how", "i", "in", "is",
    "it", "my", "of", "on", "or", "should", "the", "to", "what", "when", "with", "you", "your",
    "much", "many", "often", "day", "time", "get", "need", "pet", "pets",
    # Species words are handled by the species filter, so they shouldn't add to the score.
    "dog", "dogs", "cat", "cats", "puppy", "kitten",
}

SYSTEM_PROMPT = """You are PawPal, a pet care assistant inside a daily planning app.
Answer the owner's question using ONLY the care notes provided. Rules:
- After each fact, cite the note it came from in square brackets, like [medication.md].
- If the notes don't cover the question, say you don't have notes on that and suggest asking a vet.
- Never diagnose an illness or recommend a medicine or dose. For anything that sounds like an emergency, tell them to call their vet or an emergency clinic.
- Keep it short: under 120 words, plain sentences, no headings."""


@dataclass
class CareNote:
    """One markdown file from the knowledge folder."""

    source: str                  # file name, used for citations
    title: str
    species: str                 # "dog", "cat", or "all"
    keywords: list[str]
    min_priority: str | None     # lowest priority this kind of task should have, or None
    tip: str                     # one-line summary shown in the plan
    body: str


@dataclass
class SearchResult:
    """A note that matched a query, with how well it matched."""

    note: CareNote
    score: int
    keyword_hits: int            # matches on the note's tagged keywords (the strongest signal)


@dataclass
class Answer:
    """What ask_pawpal() returns to the UI."""

    text: str
    sources: list[str]
    used_ai: bool                # False when we fell back to showing the notes directly


# --- Loading the notes ------------------------------------------------------

def _parse_note(path: Path) -> CareNote:
    """Read a note file: a small 'key: value' header between --- lines, then the body."""
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---", 2)
    meta = {}
    for line in header.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    min_priority = meta.get("min_priority") or None
    if min_priority is not None and min_priority not in PRIORITY_ORDER:
        raise ValueError(f"{path.name}: unknown min_priority '{min_priority}'")

    return CareNote(
        source=path.name,
        title=meta.get("title", path.stem),
        species=meta.get("species", "all"),
        keywords=[k.strip().lower() for k in meta.get("keywords", "").split(",") if k.strip()],
        min_priority=min_priority,
        tip=meta.get("tip", ""),
        body=body.strip(),
    )


def load_notes(folder: Path = KNOWLEDGE_DIR) -> list[CareNote]:
    """Load every .md note in the folder. Bad files are logged and skipped, not fatal."""
    notes = []
    for path in sorted(folder.glob("*.md")):
        try:
            notes.append(_parse_note(path))
        except ValueError as error:
            logger.warning("Skipping care note %s: %s", path.name, error)
    logger.info("Loaded %d care notes from %s", len(notes), folder)
    return notes


# --- Retrieval --------------------------------------------------------------

def _words(text: str) -> set[str]:
    """Lowercase words with stop words removed and a simple plural strip ('walks' -> 'walk')."""
    words = set()
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in STOP_WORDS or len(word) < 3:
            continue
        words.add(word[:-1] if word.endswith("s") and not word.endswith("ss") else word)
    return words


class Retriever:
    """Finds the care notes that best match a query using simple keyword scoring.

    Scoring: 3 points for each tagged keyword the query mentions, 2 for each title word,
    1 for each body word. It's basic on purpose, so it's easy to explain and test, and
    the knowledge base is small enough that it doesn't need embeddings.
    """

    def __init__(self, notes: list[CareNote]):
        self.notes = notes

    def search(self, query: str, species: str | None = None, k: int = 3) -> list[SearchResult]:
        """Return up to k matching notes, best first. Notes for another species are skipped."""
        query_words = _words(query)
        if not query_words:
            return []
        results = []
        for note in self.notes:
            if species and note.species not in ("all", species):
                continue
            # Multi-word keywords like "litter box" match if every word is in the query.
            keyword_hits = sum(1 for kw in note.keywords if _words(kw) and _words(kw) <= query_words)
            score = (
                3 * keyword_hits
                + 2 * len(query_words & _words(note.title))
                + len(query_words & _words(note.body))
            )
            if score > 0:
                results.append(SearchResult(note, score, keyword_hits))

        results.sort(key=lambda r: (-r.score, r.note.source))
        logger.info(
            "search %r (species=%s) -> %s", query, species,
            [f"{r.note.source}:{r.score}" for r in results[:k]] or "no matches",
        )
        return results[:k]


# --- Feature 1: notes change the plan ---------------------------------------

@dataclass
class CareNoteChange:
    """Record of a note being applied to a task, so the UI can show what changed and why."""

    task_id: int
    task_title: str
    pet_name: str
    source: str
    tip: str
    old_priority: str
    new_priority: str

    @property
    def raised(self) -> bool:
        return self.old_priority != self.new_priority


def apply_care_notes(owner: Owner, retriever: Retriever) -> list[CareNoteChange]:
    """Match each task to a care note, and raise its priority if the note says it matters more.

    Only a tagged-keyword match counts here, so a task is never changed because of a
    loose word match in a note's body. Priorities are only ever raised, never lowered.
    """
    changes = []
    for pet in owner.pets:
        for task in pet.tasks:
            matches = [r for r in retriever.search(task.title, species=pet.species, k=1) if r.keyword_hits]
            if not matches:
                continue
            note = matches[0].note
            old = task.priority
            if note.min_priority and PRIORITY_ORDER[note.min_priority] > PRIORITY_ORDER.get(old, 0):
                task.priority = note.min_priority
                logger.info(
                    "Raised '%s' (%s) from %s to %s because of %s",
                    task.title, pet.name, old, task.priority, note.source,
                )
            changes.append(
                CareNoteChange(task.task_id, task.title, pet.name, note.source, note.tip, old, task.priority)
            )
    return changes


def plan_with_care_notes(scheduler: Scheduler, retriever: Retriever, today: date) -> list[CareNoteChange]:
    """Build today's plan with care notes applied, and add each note's tip to that task's reason.

    The raised priorities only apply to this plan. Afterwards every task goes back to the
    priority the owner entered, so the notes never silently overwrite the owner's choices.
    """
    tasks = scheduler.owner.get_all_tasks()
    original = {t.task_id: t.priority for t in tasks}
    try:
        changes = apply_care_notes(scheduler.owner, retriever)
        scheduler.generate_plan(today)
    finally:
        for t in tasks:
            t.priority = original[t.task_id]

    for change in changes:
        if change.task_id in scheduler.reasons:
            prefix = (
                f"raised from {change.old_priority} by {change.source}; "
                if change.raised else ""
            )
            scheduler.reasons[change.task_id] = (
                f"{prefix}{scheduler.reasons[change.task_id]}. Care note ({change.source}): {change.tip}"
            )
    return changes


# --- Feature 2: answer questions from the notes -----------------------------

def _pet_summary(owner: Owner) -> str:
    """One line per pet, so Claude can tailor the answer (e.g. dog vs cat)."""
    if not owner.pets:
        return "No pets added yet."
    return "\n".join(f"- {p.name}: {p.species}, {p.age} years old" for p in owner.pets)


def build_prompt(question: str, owner: Owner, results: list[SearchResult]) -> str:
    """Put the retrieved notes, the owner's pets, and the question into one message."""
    notes = "\n\n".join(f'<note source="{r.note.source}">\n{r.note.body}\n</note>' for r in results)
    return (
        f"Care notes:\n{notes}\n\n"
        f"The owner's pets:\n{_pet_summary(owner)}\n\n"
        f"Question: {question}"
    )


def _fallback_answer(results: list[SearchResult], reason: str) -> Answer:
    """When Claude can't be reached, show the matching notes' tips so the owner still gets help."""
    lines = [f"(AI answer unavailable: {reason}. Here's what the care notes say.)"]
    lines += [f"- {r.note.tip} [{r.note.source}]" for r in results]
    return Answer("\n".join(lines), [r.note.source for r in results], used_ai=False)


def ask_pawpal(question: str, owner: Owner, retriever: Retriever, client=None) -> Answer:
    """Answer a pet care question using retrieved notes. client is an anthropic.Anthropic (or a fake in tests)."""
    question = question.strip()
    if not question:
        return Answer("Please type a question first.", [], used_ai=False)

    # Search using every species the owner has, so "how often do I feed" finds dog and cat notes.
    species = {p.species for p in owner.pets} or {None}
    results_by_source = {}
    for s in species:
        for r in retriever.search(question, species=s):
            best = results_by_source.get(r.note.source)
            if best is None or r.score > best.score:
                results_by_source[r.note.source] = r
    results = sorted(results_by_source.values(), key=lambda r: -r.score)[:3]

    # Guardrail: with no matching notes, don't let the model answer from memory.
    if not results:
        logger.info("No notes for question %r; not calling the model", question)
        return Answer(
            "I don't have care notes on that yet, so I'd rather not guess. Your vet is the best person to ask.",
            [], used_ai=False,
        )

    sources = [r.note.source for r in results]
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception as error:  # SDK missing or no credentials configured
            logger.warning("Could not create Claude client: %s", error)
            return _fallback_answer(results, "Claude isn't set up")

    try:
        import anthropic
        api_errors = (anthropic.APIStatusError, anthropic.APIConnectionError)
    except ImportError:
        api_errors = ()

    try:
        logger.info("Asking %s with notes %s", MODEL, sources)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            # If the main model declines, the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": build_prompt(question, owner, results)}],
        )
    except api_errors as error:
        logger.error("Claude request failed: %s", error)
        return _fallback_answer(results, "the request to Claude failed")
    except Exception as error:  # e.g. missing API key raises before any request is sent
        logger.error("Claude request could not be sent: %s", error)
        return _fallback_answer(results, "Claude isn't set up")

    if response.stop_reason == "refusal":
        logger.warning("Claude declined the question %r", question)
        return _fallback_answer(results, "Claude declined to answer")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        logger.warning("Claude returned an empty answer (stop_reason=%s)", response.stop_reason)
        return _fallback_answer(results, "Claude returned an empty answer")

    logger.info("Answered %r using %s", question, sources)
    return Answer(text, sources, used_ai=True)

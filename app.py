"""PawPal+ Streamlit app.

Layout:
- Sidebar: owner info, add a pet, and whether Claude is set up.
- Pets & Tasks tab: one card per pet with its tasks, plus buttons to add a task or mark one done.
- Today's Plan tab: pick a planner (AI agent, care notes only, or basic) and build the plan.
- Ask PawPal tab: pet care questions answered from the care notes.
"""

import logging
import os
from datetime import date

import streamlit as st

from agent import PlanningAgent, accept_changes
from pawpal_system import Owner, Pet, Scheduler, Task
from rag import Retriever, ask_pawpal, load_notes, plan_with_care_notes

# Every retrieval, priority change, and Claude call is written here so runs can be checked later.
logging.basicConfig(
    filename="pawpal.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PRIORITY_COLORS = {"high": "red", "medium": "orange", "low": "gray"}
PLANNERS = {
    "AI agent": "Claude adjusts today's plan for your request, then the plan is checked for problems.",
    "Care notes": "Care notes raise must-do tasks, like medication. No AI call.",
    "Basic": "The original PawPal+ scheduler, using priority and time only.",
}


@st.cache_resource
def get_retriever() -> Retriever:
    """Load the care notes once instead of on every rerun."""
    return Retriever(load_notes())


retriever = get_retriever()

# Streamlit reruns this whole script on every click, so the Owner lives in
# session_state. It is only created the first time; after that we reuse it.
if "owner" not in st.session_state:
    st.session_state.owner = Owner("Jordan", available_minutes=60)

owner: Owner = st.session_state.owner
today = date.today()
scheduler = Scheduler(owner)

# Messages saved right before a rerun (like "Task added") pop up once in the corner.
if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"), icon=":material/check_circle:")


# --- Small helpers -----------------------------------------------------------

def time_label(task: Task) -> str:
    return f"{task.preferred_time:%H:%M}" if task.preferred_time else "Anytime"


def repeat_label(task: Task) -> str:
    if task.frequency == "weekly" and task.day_of_week is not None:
        return f"Weekly ({DAYS[task.day_of_week][:3]})"
    return task.frequency.title()


def task_status(task: Task) -> str:
    """Return a short label for whether a task is done, to do, or due later."""
    if task.is_completed(today):
        return "Done today"
    if task.is_due_today(today):
        return "To do"
    if task.due_date is not None and task.due_date > today:
        return f"Next due {task.due_date:%a %m/%d}"
    return "Not due today"


def flash_and_rerun(message: str) -> None:
    st.session_state.flash = message
    st.rerun()


# --- Sidebar: owner and pets -------------------------------------------------

with st.sidebar:
    st.subheader(":material/person: Owner")
    owner.name = st.text_input("Name", value=owner.name)
    owner.available_minutes = st.number_input(
        "Minutes free today", min_value=0, max_value=1440, step=5, value=owner.available_minutes
    )

    st.divider()
    st.subheader(":material/add_circle: Add a pet")
    with st.form("add_pet", clear_on_submit=True, border=False):
        pet_name = st.text_input("Pet name")
        col1, col2 = st.columns(2)
        species = col1.selectbox("Species", ["dog", "cat", "other"], format_func=str.title)
        age = col2.number_input("Age", min_value=0, max_value=40, value=1)
        if st.form_submit_button("Add pet", type="primary", width="stretch"):
            if not pet_name.strip():
                st.error("Please enter a pet name.")
            else:
                try:
                    owner.add_pet(Pet(pet_name.strip(), species, int(age)))
                    flash_and_rerun(f"Added {pet_name.strip()}")
                except ValueError as error:
                    st.error(str(error))

    st.divider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        st.caption(":material/check_circle: Claude API key found. AI features are on.")
    else:
        st.caption(
            ":material/info: No Claude API key found. The AI agent and Ask PawPal "
            "will fall back to the care notes."
        )


# --- Header and summary --------------------------------------------------------

st.title(":material/pets: PawPal+")
st.caption(f"Daily pet care planner · {today:%A, %B %d, %Y}")

cols = st.columns(4)
cols[0].metric("Pets", len(owner.pets), border=True)
cols[1].metric("To do today", len(scheduler.get_todays_tasks(today)), border=True)
cols[2].metric("Done today", len(scheduler.filter_tasks(today, completed=True)), border=True)
cols[3].metric("Minutes free", owner.available_minutes, border=True)

pets_tab, plan_tab, ask_tab = st.tabs(
    [":material/pets: Pets & Tasks", ":material/event_note: Today's Plan", ":material/forum: Ask PawPal"],
    key="main_tab",
)


# --- Tab 1: Pets & Tasks -----------------------------------------------------

def add_task_form(pet: Pet) -> None:
    """Form for adding a task to one pet (shown inside that pet's 'Add task' popover)."""
    with st.form(f"add_task_{pet.name}", clear_on_submit=True, border=False):
        title = st.text_input("Task", placeholder="Morning walk")
        col1, col2 = st.columns(2)
        duration = col1.number_input("Minutes", min_value=1, max_value=240, value=20)
        priority = col2.selectbox("Priority", ["high", "medium", "low"], format_func=str.title)
        preferred_time = col1.time_input("Time (optional)", value=None, step=900)
        frequency = col2.selectbox("Repeats", ["daily", "weekly", "once"], format_func=str.title)
        day_name = st.selectbox("Day (weekly tasks only)", DAYS)

        if st.form_submit_button("Add task", type="primary", width="stretch"):
            if not title.strip():
                st.error("Please enter a task name.")
            else:
                pet.add_task(
                    Task(
                        owner.new_task_id(),
                        title.strip(),
                        int(duration),
                        priority,
                        preferred_time=preferred_time,
                        frequency=frequency,
                        day_of_week=DAYS.index(day_name) if frequency == "weekly" else None,
                    )
                )
                flash_and_rerun(f"Added '{title.strip()}' for {pet.name}")


def mark_done_form(pet: Pet) -> None:
    """Pick one of the pet's tasks due today and mark it done."""
    # complete_task() adds the next copy for daily and weekly tasks.
    to_do = scheduler.sort_by_time([t for t in scheduler.get_todays_tasks(today) if t.pet_name == pet.name])
    if not to_do:
        st.caption("Nothing left to do today for this pet.")
        return
    labels = {t.task_id: f"{time_label(t)} · {t.title}" for t in to_do}
    done_id = st.selectbox("Task", list(labels), format_func=labels.get, key=f"done_{pet.name}")
    if st.button("Mark done", type="primary", width="stretch", key=f"done_btn_{pet.name}"):
        next_task = scheduler.complete_task(done_id, today)
        flash_and_rerun(
            f"Done. Next one is due {next_task.due_date:%A, %B %d}."
            if next_task else "Done. This was a one-time task."
        )


with pets_tab:
    if not owner.pets:
        st.info("No pets yet. Add your first pet from the sidebar.", icon=":material/pets:")
    else:
        view = st.segmented_control(
            "Show", ["All tasks", "To do today", "Done today"], default="All tasks",
            required=True, key="task_view", label_visibility="collapsed",
        )
        status_filter = {"All tasks": None, "To do today": False, "Done today": True}[view]

        for pet in owner.pets:
            with st.container(border=True):
                head, add_col, done_col = st.columns([3, 1.2, 1.2], vertical_alignment="center")
                head.markdown(f"#### {pet.name}")
                head.caption(f"{pet.species.title()} · {pet.age} yr · {len(pet.tasks)} task(s)")
                with add_col.popover("Add task", icon=":material/add:", width="stretch"):
                    add_task_form(pet)
                with done_col.popover("Mark done", icon=":material/check:", width="stretch"):
                    mark_done_form(pet)

                # filter_tasks() narrows by pet and status; sort_by_time() puts them in clock order.
                shown = scheduler.sort_by_time(
                    scheduler.filter_tasks(today, pet_name=pet.name, completed=status_filter)
                )
                if shown:
                    st.dataframe(
                        [
                            {
                                "Time": time_label(t),
                                "Task": t.title,
                                "Minutes": t.duration_minutes,
                                "Priority": t.priority.title(),
                                "Repeats": repeat_label(t),
                                "Status": task_status(t),
                            }
                            for t in shown
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                elif pet.tasks:
                    st.caption("No tasks match this view.")
                else:
                    st.caption("No tasks yet. Use **Add task** to give this pet its first one.")


# --- Tab 2: Today's Plan -----------------------------------------------------

def show_plan(plan_scheduler: Scheduler, planned_priority: dict[int, str]) -> None:
    """Show the plan as a timeline, plus conflicts and anything that didn't make it in."""
    plan = plan_scheduler.plan
    if not plan and not plan_scheduler.skipped:
        st.info("No tasks are due today.", icon=":material/event_available:")
        return

    conflicts = plan_scheduler.detect_conflicts(plan)
    used = sum(t.duration_minutes for t in plan)
    cols = st.columns(3)
    cols[0].metric("Planned", f"{len(plan)} task(s)", border=True)
    cols[1].metric("Time used", f"{used} / {owner.available_minutes} min", border=True)
    cols[2].metric("Conflicts", len(conflicts), border=True)

    # Conflicts go first, so the owner sees them before reading the plan.
    if conflicts:
        st.warning(
            "**These tasks overlap.** Change one task's time to fix it.\n\n"
            + "\n".join(f"- {c}" for c in conflicts),
            icon=":material/warning:",
        )

    for t in plan:
        with st.container(border=True):
            when, what, level = st.columns([1, 5, 1.3], vertical_alignment="center")
            when.markdown(f"**{time_label(t)}**")
            what.markdown(f"**{t.title}** · {t.pet_name} · {t.duration_minutes} min")
            what.caption(plan_scheduler.reasons[t.task_id])
            priority = planned_priority.get(t.task_id, t.priority)
            level.badge(priority.title(), color=PRIORITY_COLORS.get(priority, "gray"))

    if plan_scheduler.skipped:
        with st.expander(f"Not in today's plan ({len(plan_scheduler.skipped)})", icon=":material/event_busy:"):
            for t in plan_scheduler.skipped:
                st.markdown(f"- **{t.title}** ({t.pet_name}): {plan_scheduler.reasons[t.task_id]}")


with plan_tab:
    with st.container(border=True):
        planner = st.segmented_control(
            "Planner", list(PLANNERS), default="AI agent", required=True, key="planner"
        )
        st.caption(PLANNERS[planner])
        request = ""
        if planner == "AI agent":
            request = st.text_area(
                "Anything different about today? (optional)",
                placeholder="Vet appointment from 2 to 4pm, and it's supposed to be really hot.",
                height=80,
            )
        if st.button("Generate plan", type="primary", icon=":material/auto_awesome:", width="stretch"):
            st.session_state.show_schedule = True
            if planner == "AI agent":
                # The agent calls Claude, so it only runs on a click, not on every rerun.
                with st.spinner("The agent is planning your day..."):
                    st.session_state.agent_result = PlanningAgent(owner, retriever).run(request, today)

    if not st.session_state.get("show_schedule"):
        st.caption("Pick a planner and click **Generate plan** to see today's schedule.")

    elif planner == "AI agent" and "agent_result" in st.session_state:
        result = st.session_state.agent_result
        if result.used_ai:
            st.info(f"**Agent:** {result.summary}", icon=":material/smart_toy:")
        else:
            st.warning(
                f"The AI agent couldn't run, so this is the standard plan with care notes. {result.summary}",
                icon=":material/cloud_off:",
            )
        # Time conflicts are already shown by show_plan(), so only list the other problems here.
        for p in result.problems:
            if not p.startswith("Time conflict"):
                st.error(p, icon=":material/error:")

        show_plan(result.scheduler, {})

        with st.expander("How the agent built this plan", icon=":material/account_tree:"):
            st.code("\n".join(result.steps), language=None)

        # Human review: nothing is saved to your tasks until you accept.
        savable = [c for c in result.accepted if c.action != "skip_today"]
        if savable:
            with st.container(border=True):
                st.markdown("**Suggested changes to your tasks**")
                for c in savable:
                    st.markdown(f"- {c.describe()}: {c.reason}")
                if st.button("Accept changes", type="primary", icon=":material/check:"):
                    saved = accept_changes(owner, savable)
                    del st.session_state.agent_result
                    st.session_state.show_schedule = False
                    flash_and_rerun(f"Saved {saved} change(s) to your tasks")

    elif planner == "AI agent":
        st.caption("Click **Generate plan** to run the agent.")

    else:
        if planner == "Care notes":
            changes = plan_with_care_notes(scheduler, retriever, today)
        else:
            changes = []
            scheduler.generate_plan(today)
        # Priority each task was actually planned with (raised by a care note, or what the owner entered).
        planned_priority = {c.task_id: c.new_priority for c in changes}

        raised = [c for c in changes if c.raised]
        if raised:
            st.info(
                "**Care notes changed today's plan:**\n\n"
                + "\n".join(
                    f"- {c.task_title} ({c.pet_name}): {c.old_priority} → **{c.new_priority}**. "
                    f"{c.tip} _(source: {c.source})_"
                    for c in raised
                ),
                icon=":material/menu_book:",
            )
        show_plan(scheduler, planned_priority)


# --- Tab 3: Ask PawPal -------------------------------------------------------

with ask_tab:
    st.caption(
        "Answers come only from PawPal's pet care notes, with the source listed. "
        "This isn't a replacement for your vet."
    )
    with st.form("ask_pawpal", border=True):
        question = st.text_input("Your question", placeholder="What should I do if I miss a medication dose?")
        asked = st.form_submit_button("Ask", type="primary", icon=":material/send:")

    if asked:
        with st.spinner("Looking through the care notes..."):
            st.session_state.answer = ask_pawpal(question, owner, retriever)

    if "answer" in st.session_state:
        answer = st.session_state.answer
        with st.container(border=True):
            if answer.used_ai:
                st.markdown(answer.text)
            else:
                st.warning(answer.text, icon=":material/info:")
            if answer.sources:
                st.caption("Sources: " + ", ".join(answer.sources))

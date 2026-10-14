from __future__ import annotations

from datetime import date, datetime, time, timedelta

import streamlit as st

from aplos.database import DecisionRepository, EventRepository
from aplos.models import EventStatus
from aplos.scheduler.schedule import ApprovalError, ScheduleManager
from aplos.timeutil import localize, utcnow
from aplos.ui.common import ACTION_STYLE, card, conn, flags_html, local, settings


def _local_input(label: str, default: datetime, s, key: str) -> datetime:
    tz = s.site.tz
    d_local = default.astimezone(tz)
    c1, c2 = st.columns(2)
    d = c1.date_input(f"{label} date", d_local.date(), key=f"{key}_d")
    t = c2.time_input(f"{label} time ({s.site.timezone})", d_local.time().replace(second=0, microsecond=0),
                      step=timedelta(minutes=15), key=f"{key}_t")
    return localize(datetime.combine(d, t), tz)


def render() -> None:
    s = settings()
    c = conn()
    mgr = ScheduleManager(c, s)
    events_repo = EventRepository(c)
    st.title("Cleaning schedule")

    # ---------------------------------------------------------------- approvals
    pending = DecisionRepository(c).pending()
    st.subheader(f"Awaiting approval ({len(pending)})")
    if not pending:
        st.caption("No recommendations are waiting for an operator decision.")
    reviewer = st.text_input("Reviewer name (recorded with approvals)", value=st.session_state.get("reviewer", ""),
                             key="reviewer_input") if pending else ""
    if reviewer:
        st.session_state.reviewer = reviewer
    for d in pending:
        icon, label = ACTION_STYLE[d.final_action.value]
        detail = f"Event #{d.event_id} planned {local(d.event_time_before, s)}"
        if d.proposed_time:
            detail += f" → {'next cleaning' if d.final_action.value == 'SKIP' else 'new time'} {local(d.proposed_time, s)}"
        card(f"Recommendation #{d.id} · {local(d.created_at, s)}", f"{icon} {label}", detail,
             f'<div style="margin-top:.4rem">{d.final_reason}</div>' + flags_html(d.flags))
        a, r, note = st.columns([1, 1, 3])
        reject_note = note.text_input("Rejection note", key=f"note_{d.id}", label_visibility="collapsed",
                                      placeholder="Optional note for rejection")
        if a.button("Approve", key=f"ap_{d.id}", type="primary", disabled=not reviewer):
            try:
                mgr.approve(d.id or 0, reviewer)
                st.success(f"Recommendation #{d.id} approved and applied.")
                st.rerun()
            except ApprovalError as exc:
                st.error(str(exc))
        if r.button("Reject", key=f"rj_{d.id}", disabled=not reviewer):
            mgr.reject(d.id or 0, reviewer, reject_note or None)
            st.rerun()
    if pending and not reviewer:
        st.caption("Enter a reviewer name to approve or reject.")

    # ---------------------------------------------------------------- events
    st.subheader("Cleaning events")
    show_all = st.toggle("Include past, completed, skipped and cancelled events", value=False)
    now = utcnow()
    events = events_repo.list() if show_all else events_repo.list(now - timedelta(days=1), statuses=[EventStatus.SCHEDULED])
    if events:
        st.dataframe(
            [{
                "ID": e.id, "Status": e.status.value,
                "Original time": local(e.original_time, s), "Scheduled time": local(e.scheduled_time, s),
                "Adjusted": "yes" if e.is_adjusted else "", "Postponements": e.postpone_count,
                "Reason for last change": e.last_reason or "", "Notes": e.notes or "",
            } for e in events],
            hide_index=True, width="stretch",
        )
    else:
        st.info("No cleaning events. Add one below or generate a recurring plan.")

    tab_add, tab_edit, tab_cancel, tab_plan, tab_log = st.tabs(
        ["Add event", "Edit event", "Cancel / complete", "Recurring plan", "Change log"]
    )
    with tab_add:
        with st.form("add_event"):
            default = now + timedelta(days=2)
            when = _local_input("Cleaning", default.replace(minute=0, second=0, microsecond=0), s, "add")
            notes = st.text_input("Notes", "")
            if st.form_submit_button("Add cleaning event", type="primary"):
                if when <= now:
                    st.error("The cleaning time must be in the future.")
                else:
                    e = mgr.add_event(when, notes or None)
                    st.success(f"Added event #{e.id} at {local(e.scheduled_time, s)}")
                    st.rerun()

    scheduled = [e for e in events_repo.list(statuses=[EventStatus.SCHEDULED])]
    options = {f"#{e.id} — {local(e.scheduled_time, s)}": e for e in scheduled}
    with tab_edit:
        if not options:
            st.caption("No scheduled events to edit.")
        else:
            choice = st.selectbox("Event", list(options), key="edit_choice")
            e = options[choice]
            with st.form("edit_event"):
                when = _local_input("New", e.scheduled_time, s, f"edit{e.id}")
                notes = st.text_input("Notes", e.notes or "")
                st.caption("Editing sets a new plan: the original time and postponement count are reset, and any "
                           "pending recommendation for this event is superseded.")
                if st.form_submit_button("Save changes", type="primary"):
                    mgr.edit_event(e.id, when, notes or None)
                    st.success("Event updated.")
                    st.rerun()
    with tab_cancel:
        if not options:
            st.caption("No scheduled events.")
        else:
            choice = st.selectbox("Event", list(options), key="cancel_choice")
            e = options[choice]
            reason = st.text_input("Reason", "Cancelled by operator")
            c1, c2 = st.columns(2)
            if c1.button("Cancel event"):
                mgr.cancel_event(e.id, reason)
                st.rerun()
            if c2.button("Mark completed"):
                mgr.complete_event(e.id)
                st.rerun()
    with tab_plan:
        with st.form("plan"):
            c1, c2, c3 = st.columns(3)
            first = c1.date_input("First cleaning date", date.today() + timedelta(days=1))
            count = c2.number_input("Number of cleanings", 1, 52, 6)
            interval = c3.number_input("Interval (days)", 1, 120, s.schedule.default_interval_days)
            at = st.time_input(f"Start time ({s.site.timezone})", time.fromisoformat(s.schedule.default_cleaning_time),
                               step=timedelta(minutes=15))
            if st.form_submit_button("Create plan", type="primary"):
                created = mgr.generate_recurring(first, int(count), int(interval), at.strftime("%H:%M"))
                st.success(f"Created {len(created)} events (existing times were not duplicated).")
                st.rerun()
    with tab_log:
        changes = events_repo.changes(limit=200)
        st.dataframe(
            [{
                "When": local(datetime.fromisoformat(ch["changed_at"]), s, "%Y-%m-%d %H:%M"),
                "Event": ch["event_id"], "Change": ch["change_type"], "By": ch["actor"],
                "From": local(datetime.fromisoformat(ch["old_time"]), s) if ch["old_time"] else "",
                "To": local(datetime.fromisoformat(ch["new_time"]), s) if ch["new_time"] else "",
                "Decision": ch["decision_id"] or "", "Reason": ch["reason"] or "",
            } for ch in changes],
            hide_index=True, width="stretch",
        )

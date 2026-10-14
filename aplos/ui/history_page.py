from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from aplos.database import DecisionRepository
from aplos.models import FinalAction, Outcome
from aplos.timeutil import localize
from aplos.ui.common import ACTION_STYLE, card, conn, flags_html, local, settings, weather_tag


def render() -> None:
    s = settings()
    tz = s.site.tz
    st.title("Decision history")

    c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1])
    rng = c1.date_input("Date range", (date.today() - timedelta(days=30), date.today()))
    actions = c2.multiselect("Action", [a.value for a in FinalAction])
    outcomes = c3.multiselect("Outcome", [o.value for o in Outcome])
    weather = c4.multiselect("Weather condition", ["rain", "sand", "rain+sand", "clear", "no data"])

    start = end = None
    if isinstance(rng, tuple) and len(rng) == 2:
        start = localize(datetime.combine(rng[0], datetime.min.time()), tz)
        end = localize(datetime.combine(rng[1] + timedelta(days=1), datetime.min.time()), tz)
    rows = DecisionRepository(conn()).list(
        start, end, [FinalAction(a) for a in actions] or None, [Outcome(o) for o in outcomes] or None, limit=5000
    )
    if weather:
        rows = [d for d in rows if weather_tag(d.rules) in weather]

    table = pd.DataFrame([{
        "ID": d.id,
        "Decided": local(d.created_at, s, "%Y-%m-%d %H:%M"),
        "Event": d.event_id,
        "Original time": local(d.original_time, s, "%Y-%m-%d %H:%M"),
        "Time before decision": local(d.event_time_before, s, "%Y-%m-%d %H:%M"),
        "Action": d.final_action.value,
        "Proposed time": local(d.proposed_time, s, "%Y-%m-%d %H:%M") if d.proposed_time else "",
        "Outcome": d.outcome.value,
        "Weather": weather_tag(d.rules),
        "Forecast": d.forecast_freshness.value,
        "Forecast source": d.forecast_source or "",
        "Model": d.model_source,
        "Confidence": d.confidence,
        "Reason": d.final_reason,
        "Conditions": " | ".join(d.conditions),
        "Rules triggered": " | ".join((d.rules or {}).get("triggered_rules", [])),
        "Flags": " | ".join(d.flags),
        "Reviewed by": d.reviewed_by or "",
    } for d in rows])

    st.caption(f"{len(table)} decision(s)")
    if table.empty:
        st.info("No decisions match the filters.")
        return
    st.dataframe(table.drop(columns=["Conditions", "Rules triggered", "Flags"]), hide_index=True, width="stretch")
    st.download_button(
        "Export to CSV", table.to_csv(index=False).encode("utf-8"),
        file_name=f"aplos_decisions_{date.today().isoformat()}.csv", mime="text/csv",
    )

    st.subheader("Decision detail")
    by_id = {d.id: d for d in rows}
    chosen = st.selectbox("Decision", list(by_id), format_func=lambda i: f"#{i} · {local(by_id[i].created_at, s)} · "
                                                                       f"{by_id[i].final_action.value}")
    d = by_id[chosen]
    icon, label = ACTION_STYLE[d.final_action.value]
    card(f"Decision #{d.id} · event #{d.event_id}", f"{icon} {label} — {d.outcome.value.replace('_', ' ').title()}",
         f"Forecast {d.forecast_freshness.value} ({d.forecast_source or 'none'}, "
         f"{'' if d.forecast_age_hours is None else f'{d.forecast_age_hours:.1f} h old'}) · decision source: "
         f"{'model' if d.model and d.model.get('used_for_decision') else 'deterministic rules'}",
         f'<div style="margin-top:.4rem">{d.final_reason}</div>' + flags_html(d.flags))
    t1, t2, t3, t4 = st.tabs(["Weather inputs", "Rules", "Model output", "Raw record"])
    with t1:
        for cond in d.conditions:
            st.write(f"- {cond}")
        window = d.inputs.get("forecast_window") or []
        if window:
            st.dataframe(pd.DataFrame(window), hide_index=True, width="stretch")
        for e in d.inputs.get("forecast_errors") or []:
            st.caption(f"⚠ {e}")
    with t2:
        r = d.rules or {}
        st.write(f"**Rule default:** {r.get('deterministic_action', '—')} — {r.get('deterministic_reason', '')}")
        st.write(f"**Allowed actions:** {', '.join(r.get('allowed_actions', [])) or '—'}")
        for rule in r.get("triggered_rules", []):
            st.write(f"- {rule}")
        st.json(r, expanded=False)
    with t3:
        if d.model is None:
            st.caption("Model not called for this decision.")
        else:
            rec = d.model.get("recommendation")
            if rec:
                st.write(f"**Model ({d.model_source}) recommended:** {rec['action']} "
                         f"(confidence {rec['confidence']:.2f})")
                st.write(rec["reason"])
            if d.model_error:
                st.warning(d.model_error)
            st.json(d.model, expanded=False)
    with t4:
        st.code(json.dumps(d.model_dump(mode="json"), indent=2, default=str), language="json")

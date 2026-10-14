from __future__ import annotations

from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from aplos.database import EventRepository
from aplos.models import DataSource, EventStatus, ForecastPoint, PointKind
from aplos.timeutil import floor_hour, localize, utcnow
from aplos.ui.common import ago, cached_forecast, card, conn, freshness_pill, local, pill, settings
from aplos.weather.service import WeatherService
from aplos.weather.validation import ForecastValidationError

KIND_LABEL = {
    PointKind.FORECAST: "Forecast",
    PointKind.PAST_ESTIMATE: "Past hours (provider estimate)",
    PointKind.OBSERVATION: "Observation (manual)",
}
BLUE, ORANGE, GRID = "#2a78d6", "#eb6834", "rgba(128,128,128,0.2)"


def _chart(df: pd.DataFrame, field: str, title: str, unit: str, color: str, bars: bool, cleanings: pd.DataFrame,
           threshold: float | None = None) -> alt.LayerChart:
    base = alt.Chart(df).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%a %H:%M", gridColor=GRID, labelColor="gray")),
        tooltip=[alt.Tooltip("time:T", title="Time", format="%a %d %b %H:%M"),
                 alt.Tooltip(f"{field}:Q", title=f"{title} ({unit})", format=".1f"),
                 alt.Tooltip("type:N", title="Type")],
    )
    y = alt.Y(f"{field}:Q", title=unit, scale=alt.Scale(domainMin=0),
              axis=alt.Axis(gridColor=GRID, labelColor="gray", titleColor="gray", format=",.3~f", tickMinStep=0.1))
    mark = base.mark_bar(color=color, cornerRadiusTopLeft=2, cornerRadiusTopRight=2, size=5) if bars else \
        base.mark_line(color=color, strokeWidth=2)
    layers = [mark.encode(y=y, opacity=alt.condition(alt.datum.type == "Forecast", alt.value(1.0), alt.value(0.45)))]
    if threshold is not None:
        layers.append(alt.Chart(pd.DataFrame({"y": [threshold]})).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="y:Q"))
    if not cleanings.empty:
        layers.append(alt.Chart(cleanings).mark_rule(color="gray", strokeWidth=1.5).encode(
            x="time:T", tooltip=[alt.Tooltip("label:N", title="Cleaning")]))
    return alt.layer(*layers).properties(title=alt.TitleParams(title, anchor="start", fontSize=13), height=150)


def render() -> None:
    s = settings()
    c = conn()
    tz = s.site.tz
    st.title("Weather")
    st.caption("Weather API access requires internet connectivity. When offline, the pipeline uses the latest cached "
               "forecast or data entered manually below; nothing is fabricated.")

    left, right = st.columns([2, 1])
    with right:
        if st.button("Fetch forecast now", type="primary", width="stretch"):
            with st.spinner(f"Contacting {s.weather.provider}…"):
                result = WeatherService(c, s).get_forecast()
            if result.fetched_this_run:
                st.success(f"Retrieved {len(result.bundle.points)} periods from {result.bundle.provider}.")
            else:
                st.error("; ".join(result.errors) or "Fetch failed")
    bundle = cached_forecast(c, s)
    with left:
        if bundle is None:
            card("Latest data", "No weather data cached", "Fetch a forecast or enter data manually.")
        else:
            age = (utcnow() - bundle.fetched_at).total_seconds() / 3600
            state = "FRESH" if age <= s.rules.stale_after_hours else ("STALE" if age <= s.rules.max_forecast_age_hours else "UNAVAILABLE")
            extra = freshness_pill(state)
            if bundle.source == DataSource.MOCK:
                extra += pill("MOCK DATA", "warn")
            if bundle.source == DataSource.MANUAL:
                extra += pill("MANUAL", "info")
            card("Latest data", f"{bundle.provider} · {local(bundle.fetched_at, s)}",
                 f"Retrieved {ago(bundle.fetched_at)} · {len(bundle.points)} periods · "
                 f"{local(bundle.coverage_start, s, '%a %H:%M')} → {local(bundle.coverage_end, s, '%a %H:%M')}", extra)
            for w in bundle.warnings[:6]:
                st.caption(f"⚠ {w}")

    if bundle is not None:
        now = utcnow()
        df = pd.DataFrame([{
            "time": p.time.astimezone(tz).replace(tzinfo=None),
            "type": KIND_LABEL[p.kind],
            "rain_mm": p.precip_mm, "rain_prob": p.precip_probability_pct, "gust": p.wind_gust_kmh,
            "wind": p.wind_speed_kmh, "dust": p.dust_ugm3, "temp": p.temperature_c, "humidity": p.humidity_pct,
            "condition": p.condition or "", "dust_flag": p.dust_event_flag,
        } for p in bundle.points if p.time >= now - timedelta(hours=12)])
        evs = EventRepository(c).list(now - timedelta(hours=12), now + timedelta(days=7), [EventStatus.SCHEDULED])
        cleanings = pd.DataFrame([{"time": e.scheduled_time.astimezone(tz).replace(tzinfo=None),
                                   "label": f"#{e.id} {local(e.scheduled_time, s, '%a %H:%M')}"} for e in evs])
        st.subheader("Conditions relevant to cleaning")
        st.caption("Faded marks are past hours or observations; solid marks are forecasts. Vertical lines mark "
                   "scheduled cleanings; dashed lines are this site's thresholds.")
        r = s.rules
        charts = [
            _chart(df, "rain_mm", "Rainfall", "mm", BLUE, True, cleanings),
            _chart(df, "rain_prob", "Rain probability", "%", BLUE, False, cleanings, r.rain_probability_threshold_pct),
            _chart(df, "gust", "Wind gusts", "km/h", ORANGE, False, cleanings, r.sand_wind_gust_threshold_kmh),
        ]
        if df["dust"].notna().any():
            charts.append(_chart(df, "dust", "Dust concentration", "µg/m³", ORANGE, False, cleanings, r.dust_threshold_ugm3))
        for ch in charts:
            st.altair_chart(ch, width="stretch")
        with st.expander("Data table"):
            st.dataframe(df, hide_index=True, width="stretch")

    st.subheader("Manual weather input")
    st.caption("Use for offline operation or on-site readings. Mark rows as observations when they are measured, "
               "or forecasts when copied from another source. Stored locally and labelled as manual.")
    start_default = floor_hour(utcnow()).astimezone(tz)
    c1, c2, c3 = st.columns(3)
    start_d = c1.date_input("Start date", start_default.date(), key="man_d")
    start_t = c2.time_input(f"Start hour ({s.site.timezone})", start_default.time(), step=timedelta(hours=1), key="man_t")
    hours = c3.number_input("Hours", 6, 168, 48, key="man_h")
    kind = st.radio("Data type", ["Forecast", "Observation"], horizontal=True)
    start = localize(datetime.combine(start_d, start_t), tz)
    template = pd.DataFrame([{
        "local_time": (start + timedelta(hours=h)).astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "rain_mm": 0.0, "rain_probability_pct": None, "wind_gust_kmh": None, "dust_ugm3": None,
        "temperature_c": None, "humidity_pct": None,
    } for h in range(int(hours))])
    edited = st.data_editor(template, hide_index=True, width="stretch", key=f"editor_{start.isoformat()}_{hours}",
                            disabled=["local_time"])
    uploaded = st.file_uploader("…or upload CSV (same columns, local_time as YYYY-MM-DD HH:MM)", type=["csv"])
    note = st.text_input("Source note", "on-site rain gauge" if kind == "Observation" else "")
    if st.button("Save manual weather data"):
        frame = pd.read_csv(uploaded) if uploaded is not None else edited
        try:
            pts = []
            for row in frame.to_dict("records"):
                def num(k: str) -> float | None:
                    v = row.get(k)
                    return None if v is None or pd.isna(v) else float(v)

                pts.append(ForecastPoint(
                    time=localize(datetime.strptime(str(row["local_time"]).strip(), "%Y-%m-%d %H:%M"), tz),
                    kind=PointKind.OBSERVATION if kind == "Observation" else PointKind.FORECAST,
                    precip_mm=num("rain_mm"), precip_probability_pct=num("rain_probability_pct"),
                    wind_gust_kmh=num("wind_gust_kmh"), dust_ugm3=num("dust_ugm3"),
                    temperature_c=num("temperature_c"), humidity_pct=num("humidity_pct"), condition="Manual entry",
                ))
            saved = WeatherService(c, s, provider=_Dummy()).save_manual(pts, note=note)
            st.success(f"Saved {len(saved.points)} manual periods (forecast #{saved.id}).")
            st.rerun()
        except (ValueError, KeyError, ForecastValidationError) as exc:
            st.error(f"Could not save: {exc}")


class _Dummy:
    name = "manual"
    source = DataSource.MANUAL

    def fetch(self, latitude: float, longitude: float):  # pragma: no cover
        raise RuntimeError("not used")

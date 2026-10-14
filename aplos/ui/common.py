"""Shared helpers for the Streamlit dashboard."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import streamlit as st

from aplos.config.settings import AppSettings
from aplos.database import SettingsRepository
from aplos.models import DataSource, ForecastBundle, Freshness
from aplos.runtime import init_logging, open_db
from aplos.timeutil import fmt_local, hours_between, utcnow
from aplos.weather.service import WeatherService

ACTION_STYLE = {
    "CLEAN": ("✅", "Clean as planned"),
    "POSTPONE": ("⏸️", "Postpone"),
    "SKIP": ("⏭️", "Skip"),
    "UNDETERMINED": ("❔", "Undetermined — no change"),
}

CSS = """
<style>
.block-container {padding-top: 1.6rem; max-width: 1280px;}
.aplos-card {border: 1px solid rgba(128,128,128,.28); border-radius: 10px; padding: .9rem 1.1rem; margin-bottom: .8rem;}
.aplos-label {font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; opacity: .7; margin-bottom: .2rem;}
.aplos-value {font-size: 1.25rem; font-weight: 600; line-height: 1.3;}
.aplos-sub {font-size: .85rem; opacity: .8;}
.aplos-pill {display: inline-block; padding: .05rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 600;
             border: 1px solid currentColor; margin-right: .3rem;}
.pill-good {color: #0ca30c;} .pill-warn {color: #c98500;} .pill-bad {color: #d03b3b;} .pill-info {color: #2a78d6;}
</style>
"""


def setup_page() -> None:
    init_logging(console=False)
    st.markdown(CSS, unsafe_allow_html=True)


def conn() -> sqlite3.Connection:
    if "conn" not in st.session_state:
        st.session_state.conn = open_db()
    return st.session_state.conn


def settings() -> AppSettings:
    return SettingsRepository(conn()).load()


def local(dt: datetime | None, s: AppSettings, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    return fmt_local(dt, s.site.tz, fmt)


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    h = hours_between(dt, utcnow())
    if h < 1:
        return f"{max(0, int(h * 60))} min ago"
    if h < 48:
        return f"{h:.1f} h ago"
    return f"{h / 24:.1f} days ago"


def pill(text: str, kind: str = "info") -> str:
    return f'<span class="aplos-pill pill-{kind}">{text}</span>'


def card(label: str, value: str, sub: str = "", extra_html: str = "") -> None:
    st.markdown(
        f'<div class="aplos-card"><div class="aplos-label">{label}</div>'
        f'<div class="aplos-value">{value}</div><div class="aplos-sub">{sub}</div>{extra_html}</div>',
        unsafe_allow_html=True,
    )


def freshness_pill(f: Freshness | str | None) -> str:
    value = f.value if isinstance(f, Freshness) else (f or "UNAVAILABLE")
    kind = {"FRESH": "good", "CACHED": "info", "STALE": "warn"}.get(value, "bad")
    label = {"FRESH": "Fresh", "CACHED": "Cached (offline)", "STALE": "Stale", "UNAVAILABLE": "Unavailable"}.get(value, value)
    return pill(label, kind)


def flag_kind(flag: str) -> str:
    if flag.startswith(("STALE", "NO_TRUSTWORTHY", "INSUFFICIENT", "POSTPONEMENT_LIMIT", "MOCK", "MODEL_OVERRIDDEN")):
        return "warn"
    if flag.startswith(("AWAITING", "CACHED", "MANUAL", "MODEL_UNAVAILABLE")):
        return "info"
    return "info"


def flags_html(flags: list[str]) -> str:
    return "".join(f'<div class="aplos-sub">{pill(f.split(":")[0], flag_kind(f))} {f.partition(":")[2]}</div>' for f in flags)


def weather_tag(rules: dict[str, Any]) -> str:
    if not rules:
        return "no data"
    tags = []
    if (rules.get("rain") or {}).get("level", "none") != "none":
        tags.append("rain")
    if (rules.get("sand") or {}).get("detected"):
        tags.append("sand")
    return "+".join(tags) or "clear"


class _CacheOnly:
    """Provider stand-in so pages can read the cache without network calls."""

    name = "cache-only"

    def __init__(self, source: DataSource) -> None:
        self.source = source

    def fetch(self, latitude: float, longitude: float) -> ForecastBundle:
        raise RuntimeError("cache only")


def cached_forecast(c: sqlite3.Connection, s: AppSettings) -> ForecastBundle | None:
    source = DataSource.MOCK if s.weather.provider == "mock" else DataSource.API
    return WeatherService(c, s, provider=_CacheOnly(source)).latest_cached()

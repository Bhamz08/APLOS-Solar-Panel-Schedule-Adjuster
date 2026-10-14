"""Wiring: build a pipeline from the database settings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from aplos.config import env
from aplos.config.settings import AppSettings
from aplos.database import SettingsRepository, connect
from aplos.engine.pipeline import Pipeline
from aplos.llm.advisor import LLMAdvisor, build_advisor
from aplos.logging_setup import configure_logging
from aplos.timeutil import Clock, utcnow
from aplos.weather.base import WeatherProvider
from aplos.weather.service import WeatherService


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    return connect(path or env.db_path())


def init_logging(console: bool = True) -> None:
    configure_logging(env.log_dir(), env.log_level(), console=console)


def load_settings(conn: sqlite3.Connection) -> AppSettings:
    return SettingsRepository(conn).load()


def build_pipeline(
    conn: sqlite3.Connection,
    settings: AppSettings | None = None,
    clock: Clock = utcnow,
    provider: WeatherProvider | None = None,
    advisor: LLMAdvisor | None = None,
) -> Pipeline:
    settings = settings or load_settings(conn)
    weather = WeatherService(conn, settings, provider, clock)
    return Pipeline(conn, settings, weather, advisor or build_advisor(settings.llm), clock)

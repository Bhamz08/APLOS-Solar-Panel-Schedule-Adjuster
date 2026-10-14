"""Filesystem locations and environment variables.

Secrets (API keys) are only ever read from the environment or the local,
git-ignored `.env` file. They are never stored in SQLite.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv, set_key

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE, override=False)

ENV_DB_PATH = "APLOS_DB_PATH"
ENV_DATA_DIR = "APLOS_DATA_DIR"
ENV_LOG_DIR = "APLOS_LOG_DIR"
ENV_LOG_LEVEL = "APLOS_LOG_LEVEL"
ENV_MODEL_PATH = "APLOS_MODEL_PATH"
ENV_OPENWEATHER_KEY = "OPENWEATHER_API_KEY"


def resolve_project_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def display_path(path: str | os.PathLike[str]) -> str:
    """Path relative to the project when inside it (keeps user folders out of UI and logs)."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def data_dir() -> Path:
    path = resolve_project_path(os.environ.get(ENV_DATA_DIR, "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    override = os.environ.get(ENV_DB_PATH)
    return resolve_project_path(override) if override else data_dir() / "aplos.db"


def log_dir() -> Path:
    path = resolve_project_path(os.environ.get(ENV_LOG_DIR, "logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_level() -> str:
    return os.environ.get(ENV_LOG_LEVEL, "INFO").upper()


def model_path_override() -> str | None:
    value = os.environ.get(ENV_MODEL_PATH, "").strip()
    return value or None


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def save_secret(name: str, value: str) -> None:
    """Persist a secret to the local .env file (git-ignored) and the process env."""
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    set_key(str(ENV_FILE), name, value, quote_mode="never")
    os.environ[name] = value


def mask(secret: str | None) -> str:
    if not secret:
        return "not set"
    return "•" * 8 + secret[-4:] if len(secret) > 4 else "•" * len(secret)

"""Runtime settings read from the process environment.

The foundation does not read .env files and does not connect to a database
while settings are loaded. An empty database URL means "not configured".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ALLOWED_ENVIRONMENTS = ("local", "test", "development")
ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Settings:
    """Values the foundation is willing to use on this workstation.

    production is intentionally absent. This repository does not describe a
    live deployment, so the configuration cannot be labeled as one.
    """

    environment: str
    database_url: str | None
    log_level: str
    migrations_dir: Path


def repository_root() -> Path:
    """Return the repository root for a source checkout.

    config.py lives at src/amlog/config.py, so two parents above src is the
    repository root. This layout is the supported layout for the foundation.
    """

    return Path(__file__).resolve().parents[2]


def default_migrations_dir() -> Path:
    return repository_root() / "db" / "migrations"


def load_settings() -> Settings:
    """Read settings at call time so tests can control the environment."""

    environment = os.environ.get("AMLOG_ENVIRONMENT", "local").strip().lower()
    if environment not in ALLOWED_ENVIRONMENTS:
        allowed = ", ".join(ALLOWED_ENVIRONMENTS)
        raise ValueError(
            f"AMLOG_ENVIRONMENT must be one of: {allowed}. Received: {environment!r}."
        )

    raw_url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
    database_url = raw_url or None

    log_level = os.environ.get("AMLOG_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(ALLOWED_LOG_LEVELS)
        raise ValueError(f"AMLOG_LOG_LEVEL must be one of: {allowed}. Received: {log_level!r}.")

    raw_migrations = os.environ.get("AMLOG_MIGRATIONS_DIR", "").strip()
    migrations_dir = Path(raw_migrations) if raw_migrations else default_migrations_dir()

    return Settings(
        environment=environment,
        database_url=database_url,
        log_level=log_level,
        migrations_dir=migrations_dir,
    )

"""Tests for settings. They do not read a database or a .env file."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.config import load_settings  # noqa: E402


def isolated_environment(updates: dict[str, str] | None = None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("AMLOG_")}
    if updates:
        env.update(updates)
    return patch.dict(os.environ, env, clear=True)


class SettingsTests(unittest.TestCase):
    def test_defaults_describe_a_local_workstation_without_a_database(self) -> None:
        with isolated_environment():
            settings = load_settings()

        self.assertEqual(settings.environment, "local")
        self.assertIsNone(settings.database_url)
        self.assertEqual(settings.log_level, "INFO")
        self.assertTrue((settings.migrations_dir / "0001_initial_schema.sql").is_file())

    def test_blank_database_url_means_not_configured(self) -> None:
        with isolated_environment({"AMLOG_DATABASE_URL": "   "}):
            settings = load_settings()

        self.assertIsNone(settings.database_url)

    def test_explicit_values_are_read_without_connecting(self) -> None:
        with isolated_environment(
            {
                "AMLOG_ENVIRONMENT": "test",
                "AMLOG_DATABASE_URL": "postgresql://example.invalid/amlog",
                "AMLOG_LOG_LEVEL": "debug",
                "AMLOG_MIGRATIONS_DIR": "db/migrations",
            }
        ):
            settings = load_settings()

        self.assertEqual(settings.environment, "test")
        self.assertEqual(settings.database_url, "postgresql://example.invalid/amlog")
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.migrations_dir, Path("db/migrations"))

    def test_production_is_not_a_valid_foundation_environment(self) -> None:
        with isolated_environment({"AMLOG_ENVIRONMENT": "production"}):
            with self.assertRaises(ValueError):
                load_settings()

    def test_unknown_log_level_is_rejected(self) -> None:
        with isolated_environment({"AMLOG_LOG_LEVEL": "verbose"}):
            with self.assertRaises(ValueError):
                load_settings()


if __name__ == "__main__":
    unittest.main()

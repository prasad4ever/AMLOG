"""Tests for migration discovery and the SQL runner.

The runner is exercised with an in-memory fake connection. These tests do not
contact PostgreSQL.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.db.migrate import (  # noqa: E402
    ChecksumMismatch,
    MigrationError,
    apply_migrations,
    discover_migrations,
    main,
    split_sql,
)


class FakeConnection:
    def __init__(self, fail_on: str | None = None) -> None:
        self.applied: dict[str, str] = {}
        self.schema_exists = False
        self.statements: list[str] = []
        self.pending: tuple[str, str] | None = None
        self.commits = 0
        self.rollbacks = 0
        self.fail_on = fail_on

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        if self.pending is not None:
            version, checksum = self.pending
            self.applied[version] = checksum
            self.schema_exists = True
            self.pending = None

    def rollback(self) -> None:
        self.rollbacks += 1
        self.pending = None


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._rows: list[tuple] = []

    def execute(self, query: str, params=None) -> None:
        text = " ".join(query.split())
        self.connection.statements.append(text)
        if self.connection.fail_on and self.connection.fail_on in text:
            raise RuntimeError("forced failure")
        if "information_schema.tables" in text:
            self._rows = [(1,)] if self.connection.schema_exists else []
            return
        if text.startswith("SELECT version, checksum"):
            self._rows = list(self.connection.applied.items())
            return
        if "INSERT INTO amlog.schema_migrations" in text:
            self.connection.pending = (params[0], params[2])
            self._rows = []
            return
        self._rows = []

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows

    def close(self) -> None:
        return None


class SqlSplittingTests(unittest.TestCase):
    def test_semicolon_inside_a_function_body_stays_in_that_statement(self) -> None:
        script = """
        CREATE FUNCTION amlog.demo()
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'keep; this';
        END;
        $$;

        CREATE TABLE amlog.demo (
          note TEXT
        );
        """
        statements = split_sql(script)

        self.assertEqual(len(statements), 2)
        self.assertIn("keep; this", statements[0])
        self.assertIn("CREATE TABLE amlog.demo", statements[1])

    def test_unclosed_function_body_is_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            split_sql("CREATE FUNCTION amlog.demo() AS $$ BEGIN PERFORM 1; ")


class DiscoveryTests(unittest.TestCase):
    def test_real_migration_directory_has_the_initial_version_only(self) -> None:
        migrations = discover_migrations(ROOT / "db" / "migrations")

        self.assertEqual([migration.version for migration in migrations], ["0001"])
        self.assertEqual(migrations[0].name, "initial_schema")
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_versions_apply_in_numeric_order(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "0010_later.sql").write_text("SELECT 10;\n", encoding="utf-8")
            (folder / "0002_earlier.sql").write_text("SELECT 2;\n", encoding="utf-8")
            connection = FakeConnection()

            applied = apply_migrations(connection, folder)

        self.assertEqual(applied, ["0002", "0010"])
        combined = "\n".join(connection.statements)
        self.assertLess(combined.index("SELECT 2"), combined.index("SELECT 10"))

    def test_unexpected_file_name_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "initial.sql").write_text("SELECT 1;\n", encoding="utf-8")
            with self.assertRaises(MigrationError):
                discover_migrations(folder)

    def test_duplicate_versions_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "0001_alpha.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (folder / "0001_beta.sql").write_text("SELECT 2;\n", encoding="utf-8")
            with self.assertRaises(MigrationError):
                discover_migrations(folder)


class ApplyTests(unittest.TestCase):
    def test_a_migration_is_recorded_once_and_not_repeated(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "0001_example.sql").write_text("SELECT 1;\n", encoding="utf-8")
            connection = FakeConnection()

            first = apply_migrations(connection, folder)
            second = apply_migrations(connection, folder)

        self.assertEqual(first, ["0001"])
        self.assertEqual(second, [])
        self.assertIn("0001", connection.applied)
        self.assertEqual(connection.rollbacks, 0)
        self.assertGreaterEqual(connection.commits, 1)

    def test_edited_applied_file_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "0001_example.sql"
            original = b"SELECT 1;\n"
            path.write_bytes(original)
            connection = FakeConnection()
            apply_migrations(connection, folder)
            path.write_bytes(b"SELECT 2;\n")

            with self.assertRaises(ChecksumMismatch):
                apply_migrations(connection, folder)

        self.assertEqual(connection.applied["0001"], hashlib.sha256(original).hexdigest())

    def test_failed_statement_rolls_back_the_version_row(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "0001_example.sql").write_text("SELECT forced;\n", encoding="utf-8")
            connection = FakeConnection(fail_on="SELECT forced")

            with self.assertRaises(MigrationError):
                apply_migrations(connection, folder)

        self.assertEqual(connection.applied, {})
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_comment_only_file_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "0001_empty.sql").write_text("-- nothing to do\n", encoding="utf-8")
            with self.assertRaises(MigrationError):
                apply_migrations(FakeConnection(), folder)


class MainTests(unittest.TestCase):
    def test_missing_database_url_does_not_apply_the_schema(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("AMLOG_")}
        with patch.dict(os.environ, env, clear=True):
            status = main([])

        self.assertEqual(status, 2)

    def test_missing_driver_does_not_pretend_the_schema_was_applied(self) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            psycopg_installed = False
        else:
            psycopg_installed = True
        if psycopg_installed:
            self.skipTest("psycopg is installed, so this guard is not the active path")

        env = {key: value for key, value in os.environ.items() if not key.startswith("AMLOG_")}
        env["AMLOG_DATABASE_URL"] = "postgresql://example.invalid/amlog"
        with patch.dict(os.environ, env, clear=True):
            status = main([])

        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()

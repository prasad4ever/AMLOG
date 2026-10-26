"""Load a small fictional workload into a disposable database.

The AMLOG development database is counted before and after the test and
must stay unchanged. The test database is dropped at the end.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import psycopg
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None

from amlog.workload.generate import SCENARIO_CODES, WorkloadSpec  # noqa: E402
from amlog.workload.load import (  # noqa: E402
    load_workload,
    replace_database,
)


WORKLOAD_DATABASE = "amlog_workload_test"


def development_url() -> str:
    return os.environ.get("AMLOG_DATABASE_URL", "").strip()


class WorkloadLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = development_url()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = _development_transaction_count(url)

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not development_url():
            return
        _drop_database(cls.url, WORKLOAD_DATABASE)
        after = _development_transaction_count(cls.url)
        if after != cls.before:
            raise AssertionError(
                f"The development database changed from {cls.before} transactions to {after}."
            )

    def test_small_workload_loads_replays_and_keeps_notes_separate(self) -> None:
        spec = WorkloadSpec(
            seed=11,
            population_id="integrity",
            event_count=80,
            batch_size=25,
        )
        report = load_workload(self.url, spec, WORKLOAD_DATABASE, reset=True)
        self.assertEqual(report.recorded, 80)
        self.assertEqual(report.rejected, 0)
        self.assertEqual(report.already_recorded, 0)
        self.assertEqual(report.stored_transactions, 80)
        self.assertEqual(report.stored_annotations, 80)
        self.assertGreater(report.ingestion_seconds, 0)

        target = replace_database(self.url, WORKLOAD_DATABASE)
        connection = psycopg.connect(target, connect_timeout=5)
        try:
            self.assertEqual(connection.execute("SELECT current_database()").fetchone()[0], WORKLOAD_DATABASE)
            mismatched_owner = connection.execute(
                """
                SELECT count(*)
                FROM amlog.transactions AS t
                JOIN amlog.accounts AS a ON a.account_id = t.account_id
                WHERE t.customer_id <> a.customer_id
                """
            ).fetchone()[0]
            self.assertEqual(mismatched_owner, 0)
            before_open = connection.execute(
                """
                SELECT count(*)
                FROM amlog.transactions AS t
                JOIN amlog.accounts AS a ON a.account_id = t.account_id
                WHERE t.event_timestamp < a.opened_at
                """
            ).fetchone()[0]
            self.assertEqual(before_open, 0)
            scenarios = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT scenario_code FROM amlog_scenario.event_annotations"
                )
            }
            self.assertEqual(scenarios, set(SCENARIO_CODES))
            payment_columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'amlog' AND table_name = 'transactions'
                    """
                )
            }
            self.assertNotIn("scenario_code", payment_columns)
            self.assertNotIn("label_value", payment_columns)
            profiles = connection.execute(
                "SELECT count(DISTINCT profile_code) FROM amlog_scenario.event_annotations"
            ).fetchone()[0]
            self.assertGreaterEqual(profiles, 7)
            channels = connection.execute(
                "SELECT count(DISTINCT payment_channel) FROM amlog.transactions"
            ).fetchone()[0]
            self.assertGreaterEqual(channels, 4)
        finally:
            connection.close()

        replay = load_workload(self.url, spec, WORKLOAD_DATABASE, reset=False)
        self.assertEqual(replay.recorded, 0)
        self.assertEqual(replay.already_recorded, 80)
        self.assertEqual(replay.rejected, 0)
        self.assertEqual(replay.stored_transactions, 80)
        self.assertEqual(replay.stored_annotations, 80)
        self.assertEqual(_development_transaction_count(self.url), self.before)


def _development_transaction_count(url: str) -> int:
    connection = psycopg.connect(url, connect_timeout=5)
    try:
        current = connection.execute("SELECT current_database()").fetchone()[0]
        if current != "amlog":
            raise AssertionError(f"The development URL points at {current!r}, not 'amlog'.")
        return connection.execute("SELECT count(*) FROM amlog.transactions").fetchone()[0]
    finally:
        connection.close()


def _drop_database(url: str, database_name: str) -> None:
    from psycopg import sql

    if database_name == "amlog":
        raise AssertionError("The test tried to drop the development database.")
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

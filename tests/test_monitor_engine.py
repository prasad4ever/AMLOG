"""Replay R001 against an isolated database.

The development database is only used to prove the engine refuses it.
amlog_monitor_test is created for these rows and dropped afterwards.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Json
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None
    sql = None
    Json = None

from amlog.db.migrate import apply_migrations  # noqa: E402
from amlog.config import default_migrations_dir  # noqa: E402
from amlog.monitor.engine import (  # noqa: E402
    MonitoringError,
    assert_replay_database,
    run_r001_replay,
    run_replay,
)
from amlog.monitor.velocity import R001_CODE, R001_VERSION  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402


MONITOR_DATABASE = "amlog_monitor_test"
ORIGIN = datetime(2025, 6, 15, 9, 0, tzinfo=timezone.utc)


def development_url() -> str:
    return os.environ.get("AMLOG_DATABASE_URL", "").strip()


class MonitoringEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = development_url()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = _development_counts(url)
        _recreate_database(url)
        with psycopg.connect(replace_database(url, MONITOR_DATABASE), connect_timeout=5) as connection:
            apply_migrations(connection, default_migrations_dir())

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not development_url():
            return
        _drop_database(cls.url)
        after = _development_counts(cls.url)
        if after != cls.before:
            raise AssertionError(
                f"The development database changed from {cls.before} to {after}."
            )

    def test_development_database_is_refused_before_any_rule_is_written(self) -> None:
        with self.assertRaises(MonitoringError):
            assert_replay_database("amlog")
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            self.assertEqual(connection.execute("SELECT current_database()").fetchone()[0], "amlog")
            with self.assertRaises(MonitoringError):
                run_r001_replay(connection)

    def test_six_debits_open_one_alert_and_five_do_not(self) -> None:
        quiet = self._account()
        self._debits(quiet, 5, gap_minutes=5)
        loud = self._account()
        identifiers = self._debits(loud, 6, gap_minutes=5)
        first = self._replay()
        self.assertGreaterEqual(first.alerts_recorded, 1)
        self.assertEqual(self._alert_count(quiet), 0)
        self.assertEqual(self._alert_count(loud), 1)
        self._assert_links(loud, identifiers)

    def test_exact_sixty_minute_boundary_and_one_second_beyond_it(self) -> None:
        inside = self._account()
        inside_ids = self._debits(inside, 5, gap_minutes=10)
        inside_ids.append(self._debit(inside, minutes=60))
        outside = self._account()
        self._debits(outside, 5, gap_minutes=10)
        self._debit(outside, minutes=60, seconds=1)
        self._replay()
        self.assertEqual(self._alert_count(inside), 1)
        self.assertEqual(self._alert_count(outside), 0)
        self._assert_links(inside, inside_ids)

    def test_credits_do_not_count(self) -> None:
        account_id, _customer_id = self._account()
        for index in range(8):
            self._insert(account_id, direction="credit", event_time=ORIGIN + timedelta(minutes=index))
        self._debits((account_id, _customer_id), 5, gap_minutes=2)
        self._replay()
        self.assertEqual(self._alert_count(account_id), 0)

    def test_events_are_evaluated_by_event_time_rather_than_insert_order(self) -> None:
        account_id, _customer_id = self._account()
        inserted = []
        for minutes in (50, 40, 30, 20, 10, 0):
            inserted.append(self._debit((account_id, _customer_id), minutes=minutes))
        self._replay()
        alert = self._alert(account_id)
        triggering = self._triggering_transaction(alert[0])
        self.assertEqual(triggering[1], ORIGIN + timedelta(minutes=50))
        linked_times = self._linked_times(alert[0])
        self.assertEqual(linked_times, tuple(sorted(linked_times)))
        self.assertLess(max(linked_times), ORIGIN + timedelta(minutes=51))
        self.assertEqual(len(inserted), 6)

    def test_a_later_debit_outside_the_window_does_not_complete_an_earlier_group(self) -> None:
        account = self._account()
        self._debits(account, 5, gap_minutes=2)
        self._debit(account, minutes=180)
        self._replay()
        self.assertEqual(self._alert_count(account), 0)

    def test_continuation_is_one_alert_and_a_later_gap_can_open_another(self) -> None:
        continued = self._account()
        self._debits(continued, 7, gap_minutes=1)
        separated = self._account()
        self._debits(separated, 6, gap_minutes=1)
        self._debits(separated, 6, gap_minutes=1, start_minutes=180)
        self._replay()
        self.assertEqual(self._alert_count(continued), 1)
        self.assertEqual(self._link_count(continued), 6)
        self.assertEqual(self._alert_count(separated), 2)
        self.assertEqual(self._link_count(separated), 12)

    def test_running_the_replay_again_does_not_duplicate_the_alert(self) -> None:
        account = self._account()
        self._debits(account, 6, gap_minutes=3)
        first = self._replay()
        audits = self._audit_count(account)
        links = self._link_count(account)
        second = self._replay()
        self.assertEqual(self._alert_count(account), 1)
        self.assertEqual(second.alerts_recorded, 0)
        self.assertGreaterEqual(second.alerts_already_present, 1)
        self.assertGreaterEqual(first.alerts_recorded, 1)
        self.assertEqual(self._link_count(account), links)
        self.assertEqual(self._audit_count(account), audits)
        alert = self._alert(account)
        self.assertIn("Historical replay", alert[1])
        self.assertIn("not produced by live monitoring", alert[1])
        self.assertEqual(alert[2], R001_CODE)
        self.assertEqual(alert[3], R001_VERSION)

    def test_activity_before_the_rule_effective_time_is_out_of_scope(self) -> None:
        account = self._account()
        early = datetime(2024, 12, 31, 23, 0, tzinfo=timezone.utc)
        for index in range(6):
            self._insert(account[0], direction="debit", event_time=early + timedelta(minutes=index))
        self._replay()
        self.assertEqual(self._alert_count(account), 0)

    def test_a_different_rule_version_uses_its_own_threshold(self) -> None:
        account = self._account()
        identifiers = self._debits(account, 3, gap_minutes=0.5)
        self._insert_version_two(minimum_count=3, window_seconds=120)
        version_one = self._replay()
        version_two = self._replay_version(2)
        self.assertEqual(self._alert_count(account), 0)
        self.assertEqual(self._alert_count(account, version=2), 1)
        self.assertGreaterEqual(version_one.transactions_considered, 3)
        self.assertGreaterEqual(version_two.alerts_recorded, 1)
        alert = self._alert(account, version=2)
        self.assertEqual(alert[3], 2)
        self.assertEqual(self._link_count(account, version=2), 3)
        self.assertEqual(len(identifiers), 3)

    def test_scenario_notes_are_not_an_input(self) -> None:
        benign = self._account()
        self._debits(benign, 6, gap_minutes=2)
        quiet = self._account()
        self._debits(quiet, 2, gap_minutes=2)
        self._note(benign[0], "high_frequency")
        self._note(quiet[0], "rapid_movement")
        self._replay()
        self.assertEqual(self._alert_count(benign), 1)
        self.assertEqual(self._alert_count(quiet), 0)

    def _replay(self):
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return run_r001_replay(connection)

    def _replay_version(self, version_number: int):
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return run_replay(connection, R001_CODE, version_number)

    def _account(self) -> tuple[uuid.UUID, uuid.UUID]:
        token = uuid.uuid4().hex
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                """
                INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
                VALUES (%s, 'individual', 'Monitor test subject')
                RETURNING customer_id
                """,
                (f"monitor-{token}",),
            ).fetchone()[0]
            account_id = connection.execute(
                """
                INSERT INTO amlog.accounts (
                    customer_id,
                    account_reference,
                    source_system,
                    source_account_id,
                    account_type,
                    currency_code
                )
                VALUES (%s, %s, 'amlog-monitor-test', %s, 'deposit', 'USD')
                RETURNING account_id
                """,
                (customer_id, f"monitor-account-{token}", token),
            ).fetchone()[0]
        return account_id, customer_id

    def _debits(
        self,
        account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID,
        count: int,
        *,
        gap_minutes: float,
        start_minutes: float = 0,
    ) -> list[uuid.UUID]:
        account_id = account[0] if isinstance(account, tuple) else account
        identifiers = []
        for index in range(count):
            identifiers.append(
                self._debit(account_id, minutes=start_minutes + (index * gap_minutes))
            )
        return identifiers

    def _debit(
        self,
        account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID,
        *,
        minutes: float,
        seconds: float = 0,
    ) -> uuid.UUID:
        account_id = account[0] if isinstance(account, tuple) else account
        return self._insert(
            account_id,
            direction="debit",
            event_time=ORIGIN + timedelta(minutes=minutes, seconds=seconds),
        )

    def _insert(self, account_id: uuid.UUID, *, direction: str, event_time: datetime) -> uuid.UUID:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                "SELECT customer_id FROM amlog.accounts WHERE account_id = %s",
                (account_id,),
            ).fetchone()[0]
            row = connection.execute(
                """
                INSERT INTO amlog.transactions (
                    source_system,
                    source_event_id,
                    account_id,
                    customer_id,
                    payment_channel,
                    transaction_type,
                    direction,
                    amount,
                    currency_code,
                    event_timestamp
                )
                VALUES ('amlog-monitor-test', %s, %s, %s, 'card', 'purchase', %s, %s, 'USD', %s)
                RETURNING transaction_id
                """,
                (
                    uuid.uuid4().hex,
                    account_id,
                    customer_id,
                    direction,
                    Decimal("10.00"),
                    event_time,
                ),
            ).fetchone()
        return row[0]

    def _insert_version_two(self, *, minimum_count: int, window_seconds: int) -> None:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            exists = connection.execute(
                """
                SELECT 1
                FROM amlog.monitoring_rules
                WHERE rule_code = 'R001' AND version_number = 2
                """
            ).fetchone()
            if exists is not None:
                return
            rule_id = connection.execute(
                """
                INSERT INTO amlog.monitoring_rules (
                    rule_code,
                    version_number,
                    rule_name,
                    description,
                    parameter_document,
                    severity,
                    lifecycle_status,
                    effective_from,
                    authored_by
                )
                VALUES (
                    'R001', 2, 'Outgoing transaction velocity',
                    'Test version with its own threshold.',
                    %s::jsonb, 'medium', 'draft', %s, 'amlog.monitoring'
                )
                RETURNING monitoring_rule_id
                """,
                (
                    Json(
                        {
                            "direction": "debit",
                            "window_seconds": window_seconds,
                            "minimum_count": minimum_count,
                        }
                    ),
                    datetime(2025, 1, 1, tzinfo=timezone.utc),
                ),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE amlog.monitoring_rules
                   SET lifecycle_status = 'active'
                 WHERE monitoring_rule_id = %s
                """,
                (rule_id,),
            )

    def _note(self, account_id: uuid.UUID, scenario_code: str) -> None:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            connection.execute("CREATE SCHEMA IF NOT EXISTS amlog_scenario")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amlog_scenario.ignored_notes (
                    account_id UUID PRIMARY KEY,
                    scenario_code TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO amlog_scenario.ignored_notes (account_id, scenario_code)
                VALUES (%s, %s)
                ON CONFLICT (account_id) DO NOTHING
                """,
                (account_id, scenario_code),
            )

    def _alert_count(self, account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID, version: int = 1) -> int:
        account_id = account[0] if isinstance(account, tuple) else account
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.alerts
                WHERE account_id = %s
                  AND recorded_rule_version = %s
                """,
                (account_id, version),
            ).fetchone()[0]

    def _alert(self, account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID, version: int = 1):
        account_id = account[0] if isinstance(account, tuple) else account
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT alert_id, summary, recorded_rule_code, recorded_rule_version, triggered_at
                FROM amlog.alerts
                WHERE account_id = %s
                  AND recorded_rule_version = %s
                """,
                (account_id, version),
            ).fetchone()

    def _assert_links(self, account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID, transaction_ids: list[uuid.UUID]) -> None:
        account_id = account[0] if isinstance(account, tuple) else account
        alert = self._alert(account)
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT link.transaction_id, link.relationship_role, payment.direction
                FROM amlog.alert_transactions AS link
                JOIN amlog.transactions AS payment ON payment.transaction_id = link.transaction_id
                WHERE link.alert_id = %s
                ORDER BY payment.event_timestamp, payment.transaction_id
                """,
                (alert[0],),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], transaction_ids)
        self.assertEqual([row[1] for row in rows], ["supporting"] * (len(rows) - 1) + ["triggering"])
        self.assertTrue(all(row[2] == "debit" for row in rows))
        reference = f"R001-v1-{account_id}-" + str(transaction_ids[-1])
        self.assertEqual(self._reference(alert[0]), reference)

    def _reference(self, alert_id: uuid.UUID) -> str:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                "SELECT alert_reference FROM amlog.alerts WHERE alert_id = %s",
                (alert_id,),
            ).fetchone()[0]

    def _link_count(self, account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID, version: int = 1) -> int:
        account_id = account[0] if isinstance(account, tuple) else account
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.alert_transactions AS link
                JOIN amlog.alerts AS alert ON alert.alert_id = link.alert_id
                WHERE alert.account_id = %s
                  AND alert.recorded_rule_version = %s
                """,
                (account_id, version),
            ).fetchone()[0]

    def _triggering_transaction(self, alert_id: uuid.UUID):
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT payment.transaction_id, payment.event_timestamp
                FROM amlog.alert_transactions AS link
                JOIN amlog.transactions AS payment ON payment.transaction_id = link.transaction_id
                WHERE link.alert_id = %s
                  AND link.relationship_role = 'triggering'
                """,
                (alert_id,),
            ).fetchone()

    def _linked_times(self, alert_id: uuid.UUID) -> tuple[datetime, ...]:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT payment.event_timestamp
                FROM amlog.alert_transactions AS link
                JOIN amlog.transactions AS payment ON payment.transaction_id = link.transaction_id
                WHERE link.alert_id = %s
                ORDER BY payment.event_timestamp
                """,
                (alert_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def _audit_count(self, account: tuple[uuid.UUID, uuid.UUID] | uuid.UUID) -> int:
        account_id = account[0] if isinstance(account, tuple) else account
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.audit_events AS audit
                JOIN amlog.alerts AS alert ON alert.alert_id = audit.entity_id
                WHERE audit.entity_table = 'alerts'
                  AND audit.action = 'alert_recorded'
                  AND alert.account_id = %s
                """,
                (account_id,),
            ).fetchone()[0]


def _development_counts(url: str) -> tuple[int, int, int]:
    with psycopg.connect(url, connect_timeout=5) as connection:
        current = connection.execute("SELECT current_database()").fetchone()[0]
        if current != "amlog":
            raise AssertionError(f"AMLOG_DATABASE_URL points at {current}, not amlog.")
        transactions = connection.execute("SELECT count(*) FROM amlog.transactions").fetchone()[0]
        rules = connection.execute("SELECT count(*) FROM amlog.monitoring_rules").fetchone()[0]
        alerts = connection.execute("SELECT count(*) FROM amlog.alerts").fetchone()[0]
    return int(transactions), int(rules), int(alerts)


def _recreate_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(MONITOR_DATABASE))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(MONITOR_DATABASE)))
    finally:
        admin.close()


def _drop_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(MONITOR_DATABASE))
        )
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

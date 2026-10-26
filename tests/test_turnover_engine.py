"""Replay R002 against an isolated database.

The development database is only used to prove the engine refuses it.
amlog_r002_test is created for these rows and dropped afterwards.
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
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None
    sql = None

from amlog.config import default_migrations_dir  # noqa: E402
from amlog.db.migrate import apply_migrations  # noqa: E402
from amlog.monitor.engine import (  # noqa: E402
    MonitoringError,
    assert_replay_database,
    run_r001_replay,
    run_r002_replay,
)
from amlog.workload.load import replace_database  # noqa: E402


MONITOR_DATABASE = "amlog_r002_test"
ORIGIN = datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc)


def development_url() -> str:
    return os.environ.get("AMLOG_DATABASE_URL", "").strip()


class TurnoverEngineTests(unittest.TestCase):
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

    def test_development_database_is_refused(self) -> None:
        with self.assertRaises(MonitoringError):
            assert_replay_database("amlog")
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            with self.assertRaises(MonitoringError):
                run_r002_replay(connection)

    def test_exact_boundary_is_stored_with_evidence_roles(self) -> None:
        account = self._account()
        incoming = self._payment(account, direction="credit", amount="500.00", hours=0)
        outgoing = self._payment(account, direction="debit", amount="400.00", hours=24)
        self._replay()
        alert = self._alert(account)
        self.assertEqual(alert[2], "R002")
        self.assertEqual(alert[3], 1)
        self.assertEqual(alert[4], ORIGIN + timedelta(hours=24))
        self.assertEqual(alert[5], ORIGIN)
        self.assertIn("does not prove that the same funds were transferred", alert[1])
        self.assertIn("Historical replay", alert[1])
        links = self._links(alert[0])
        self.assertEqual(links, [(incoming, "contextual"), (outgoing, "triggering")])

    def test_one_second_late_a_shortfall_and_another_currency_do_not_alert(self) -> None:
        late = self._account()
        self._payment(late, direction="credit", amount="500.00", hours=0)
        self._payment(late, direction="debit", amount="400.00", hours=24, seconds=1)
        short = self._account()
        self._payment(short, direction="credit", amount="500.00", hours=0)
        self._payment(short, direction="debit", amount="399.99", hours=1)
        foreign = self._account()
        self._payment(foreign, direction="credit", amount="1000.00", hours=0, currency="USD")
        self._payment(
            foreign,
            direction="debit",
            amount="1000.00",
            hours=2,
            currency="EUR",
            base_currency="USD",
            base_amount="1000.00",
            fx_rate="1.00000000",
        )
        self._replay()
        self.assertEqual(self._alert_count(late), 0)
        self.assertEqual(self._alert_count(short), 0)
        self.assertEqual(self._alert_count(foreign), 0)

    def test_several_payments_and_a_repeated_replay(self) -> None:
        account = self._account()
        incoming = self._payment(account, direction="credit", amount="500.00", hours=0)
        first = self._payment(account, direction="debit", amount="200.00", hours=1)
        second = self._payment(account, direction="debit", amount="200.00", hours=2)
        self._payment(account, direction="debit", amount="100.00", hours=3)
        first_run = self._replay()
        links = self._link_count(account)
        audits = self._audit_count(account)
        second_run = self._replay()
        self.assertEqual(self._alert_count(account), 1)
        self.assertGreaterEqual(first_run.alerts_recorded, 1)
        self.assertEqual(second_run.alerts_recorded, 0)
        self.assertGreaterEqual(second_run.alerts_already_present, 1)
        self.assertEqual(self._link_count(account), links)
        self.assertEqual(self._audit_count(account), audits)
        alert = self._alert(account)
        self.assertEqual(
            self._links(alert[0]),
            [(incoming, "contextual"), (first, "supporting"), (second, "triggering")],
        )
        self.assertEqual(
            alert[6],
            f"R002-v1-{account[0]}-{incoming}",
        )

    def test_running_r002_leaves_an_r001_alert_unchanged(self) -> None:
        velocity = self._account()
        for index in range(6):
            self._payment(velocity, direction="debit", amount="10.00", hours=index * 0.1)
        turnover = self._account()
        self._payment(turnover, direction="credit", amount="800.00", hours=0)
        self._payment(turnover, direction="debit", amount="700.00", hours=4)
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            run_r001_replay(connection)
        before = self._rule_alerts("R001")
        self._replay()
        after = self._rule_alerts("R001")
        self.assertEqual(before, after)
        self.assertEqual(len(before), 1)
        self.assertEqual(self._alert_count(turnover), 1)
        self.assertEqual(self._alert_count(velocity, code="R002"), 0)

    def test_activity_before_the_effective_time_and_scenario_notes_are_ignored(self) -> None:
        early = self._account()
        early_time = datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)
        self._payment(early, direction="credit", amount="900.00", at=early_time)
        self._payment(early, direction="debit", amount="900.00", at=early_time + timedelta(hours=2))
        noted = self._account()
        self._payment(noted, direction="credit", amount="900.00", hours=0)
        self._payment(noted, direction="debit", amount="900.00", hours=2)
        self._note(noted[0], "rapid_movement")
        quiet = self._account()
        self._payment(quiet, direction="credit", amount="100.00", hours=0)
        self._note(quiet[0], "rapid_movement")
        self._replay()
        self.assertEqual(self._alert_count(early), 0)
        self.assertEqual(self._alert_count(noted), 1)
        self.assertEqual(self._alert_count(quiet), 0)

    def _replay(self):
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return run_r002_replay(connection)

    def _account(self) -> tuple[uuid.UUID, uuid.UUID]:
        token = uuid.uuid4().hex
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                """
                INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
                VALUES (%s, 'individual', 'Turnover test subject')
                RETURNING customer_id
                """,
                (f"turnover-{token}",),
            ).fetchone()[0]
            account_id = connection.execute(
                """
                INSERT INTO amlog.accounts (
                    customer_id, account_reference, source_system, source_account_id,
                    account_type, currency_code
                )
                VALUES (%s, %s, 'amlog-turnover-test', %s, 'deposit', 'USD')
                RETURNING account_id
                """,
                (customer_id, f"turnover-account-{token}", token),
            ).fetchone()[0]
        return account_id, customer_id

    def _payment(
        self,
        account: tuple[uuid.UUID, uuid.UUID],
        *,
        direction: str,
        amount: str,
        hours: float = 0,
        seconds: float = 0,
        currency: str = "USD",
        at: datetime | None = None,
        base_currency: str | None = None,
        base_amount: str | None = None,
        fx_rate: str | None = None,
    ) -> uuid.UUID:
        event_time = at if at is not None else ORIGIN + timedelta(hours=hours, seconds=seconds)
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            row = connection.execute(
                """
                INSERT INTO amlog.transactions (
                    source_system, source_event_id, account_id, customer_id,
                    payment_channel, transaction_type, direction, amount, currency_code,
                    base_currency_code, base_amount, fx_rate, event_timestamp
                )
                VALUES (
                    'amlog-turnover-test', %s, %s, %s, 'wire', 'transfer', %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING transaction_id
                """,
                (
                    uuid.uuid4().hex,
                    account[0],
                    account[1],
                    direction,
                    Decimal(amount),
                    currency,
                    base_currency,
                    Decimal(base_amount) if base_amount is not None else None,
                    Decimal(fx_rate) if fx_rate is not None else None,
                    event_time,
                ),
            ).fetchone()
        return row[0]

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

    def _alert_count(self, account: tuple[uuid.UUID, uuid.UUID], code: str = "R002") -> int:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.alerts
                WHERE account_id = %s AND recorded_rule_code = %s
                """,
                (account[0], code),
            ).fetchone()[0]

    def _alert(self, account: tuple[uuid.UUID, uuid.UUID]):
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT
                    alert_id, summary, recorded_rule_code, recorded_rule_version,
                    triggered_at, window_started_at, alert_reference
                FROM amlog.alerts
                WHERE account_id = %s AND recorded_rule_code = 'R002'
                """,
                (account[0],),
            ).fetchone()

    def _links(self, alert_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT link.transaction_id, link.relationship_role
                FROM amlog.alert_transactions AS link
                JOIN amlog.transactions AS payment ON payment.transaction_id = link.transaction_id
                WHERE link.alert_id = %s
                ORDER BY payment.event_timestamp, payment.transaction_id
                """,
                (alert_id,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _link_count(self, account: tuple[uuid.UUID, uuid.UUID]) -> int:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.alert_transactions AS link
                JOIN amlog.alerts AS alert ON alert.alert_id = link.alert_id
                WHERE alert.account_id = %s AND alert.recorded_rule_code = 'R002'
                """,
                (account[0],),
            ).fetchone()[0]

    def _audit_count(self, account: tuple[uuid.UUID, uuid.UUID]) -> int:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT count(*)
                FROM amlog.audit_events AS audit
                JOIN amlog.alerts AS alert ON alert.alert_id = audit.entity_id
                WHERE audit.action = 'alert_recorded'
                  AND alert.account_id = %s
                  AND alert.recorded_rule_code = 'R002'
                """,
                (account[0],),
            ).fetchone()[0]

    def _rule_alerts(self, rule_code: str) -> tuple[tuple[object, ...], ...]:
        with psycopg.connect(replace_database(self.url, MONITOR_DATABASE), connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT alert_id, alert_reference, summary, triggered_at, window_started_at, window_ended_at
                FROM amlog.alerts
                WHERE recorded_rule_code = %s
                ORDER BY alert_reference
                """,
                (rule_code,),
            ).fetchall()
        return tuple(rows)


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
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(MONITOR_DATABASE)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(MONITOR_DATABASE)))
    finally:
        admin.close()


def _drop_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(MONITOR_DATABASE)))
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

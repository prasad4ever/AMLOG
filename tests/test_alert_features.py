"""Point-in-time features for one alert.

Pure tests do not connect to PostgreSQL. Persistence tests use
amlog_feature_test and drop it afterwards.
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
from amlog.features.compute import (  # noqa: E402
    CORE_FEATURE_NAMES,
    FEATURE_SET_NAME,
    FEATURE_SET_VERSION,
    AlertView,
    PaymentView,
    compute_snapshot,
)
from amlog.features.store import (  # noqa: E402
    _ALERT_QUERY,
    _PAYMENT_QUERY,
    FeatureError,
    assert_feature_database,
    run_feature_snapshots,
)
from amlog.monitor.engine import register_r001  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402

FEATURE_DATABASE = "amlog_feature_test"
WHEN = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def _alert(account_id: uuid.UUID, when: datetime = WHEN, **overrides) -> AlertView:
    values = {
        "alert_id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "account_id": account_id,
        "customer_id": uuid.UUID("00000000-0000-4000-8000-000000000010"),
        "triggered_at": when,
        "window_started_at": when - timedelta(hours=1),
        "window_ended_at": when,
        "rule_code": "R001",
        "rule_version": 1,
        "account_opened_at": when - timedelta(days=400),
        "account_currency": "USD",
        "evidence_count": 1,
        "triggering_transaction_id": None,
    }
    values.update(overrides)
    return AlertView(**values)


def _payment(
    account_id: uuid.UUID,
    when: datetime,
    *,
    direction: str = "debit",
    amount: str = "10.00",
    currency: str = "USD",
    channel: str = "card",
    counterparty: str | None = "merchant-1",
    transaction_id: uuid.UUID | None = None,
) -> PaymentView:
    return PaymentView(
        transaction_id=transaction_id or uuid.uuid4(),
        account_id=account_id,
        event_time=when,
        direction=direction,
        amount=Decimal(amount),
        currency_code=currency,
        payment_channel=channel,
        counterparty_reference=counterparty,
    )


class FeatureCalculationTests(unittest.TestCase):
    def test_a_payment_after_the_alert_is_invisible(self) -> None:
        account = uuid.uuid4()
        present = _payment(account, WHEN)
        future = _payment(account, WHEN + timedelta(seconds=1), amount="999.00")
        snapshot = compute_snapshot(_alert(account), [present, future])
        values = snapshot.by_name()
        self.assertEqual(values["payment_count_1h"].numeric, Decimal(1))
        self.assertEqual(values["payment_count_30d"].numeric, Decimal(1))
        self.assertNotIn("999", str(values.get("same_currency_debit_sum_24h").numeric))

    def test_window_boundaries_are_closed_at_both_ends(self) -> None:
        account = uuid.uuid4()
        payments = [
            _payment(account, WHEN),
            _payment(account, WHEN - timedelta(hours=1)),
            _payment(account, WHEN - timedelta(hours=1, seconds=1), amount="3.00"),
            _payment(account, WHEN - timedelta(days=7)),
            _payment(account, WHEN - timedelta(days=7, seconds=1), amount="4.00"),
            _payment(account, WHEN - timedelta(days=30)),
            _payment(account, WHEN - timedelta(days=30, seconds=1), amount="5.00"),
        ]
        values = compute_snapshot(_alert(account), payments).by_name()
        self.assertEqual(values["payment_count_1h"].numeric, Decimal(2))
        self.assertEqual(values["payment_count_24h"].numeric, Decimal(3))
        self.assertEqual(values["payment_count_7d"].numeric, Decimal(4))
        self.assertEqual(values["payment_count_30d"].numeric, Decimal(6))

    def test_another_account_does_not_change_the_snapshot(self) -> None:
        account = uuid.uuid4()
        other = uuid.uuid4()
        payments = [
            _payment(account, WHEN, amount="10.00"),
            _payment(other, WHEN, amount="80.00", currency="EUR"),
        ]
        values = compute_snapshot(_alert(account), payments).by_name()
        self.assertEqual(values["payment_count_24h"].numeric, Decimal(1))
        self.assertEqual(values["same_currency_debit_sum_24h"].numeric, Decimal("10.00000000"))
        self.assertEqual(values["other_currency_count_24h"].numeric, Decimal(0))

    def test_amounts_in_another_currency_are_not_added(self) -> None:
        account = uuid.uuid4()
        payments = [
            _payment(account, WHEN, amount="10.00", currency="USD"),
            _payment(account, WHEN - timedelta(minutes=5), amount="50.00", currency="EUR", channel="wire"),
        ]
        values = compute_snapshot(_alert(account), payments).by_name()
        self.assertEqual(values["same_currency_debit_sum_24h"].numeric, Decimal("10.00000000"))
        self.assertEqual(values["other_currency_count_24h"].numeric, Decimal(1))
        self.assertEqual(values["distinct_currency_count_30d"].numeric, Decimal(2))
        self.assertEqual(values["wire_count_24h"].numeric, Decimal(1))
        self.assertNotIn("fx_rate", values)

    def test_a_short_history_is_not_described_as_a_full_quiet_month(self) -> None:
        account = uuid.uuid4()
        alert = _alert(account, account_opened_at=WHEN - timedelta(hours=2))
        payments = [_payment(account, WHEN - timedelta(minutes=30), counterparty="new-shop")]
        values = compute_snapshot(alert, payments).by_name()
        self.assertTrue(values["history_covers_1h"].boolean)
        self.assertFalse(values["history_covers_24h"].boolean)
        self.assertFalse(values["history_covers_7d"].boolean)
        self.assertFalse(values["history_covers_30d"].boolean)
        self.assertEqual(values["payment_count_30d"].numeric, Decimal(1))
        self.assertNotIn("new_counterparty_count_24h", values)
        self.assertNotIn("debit_daily_rate_24h_vs_prior_6d", values)

    def test_a_zero_baseline_does_not_become_a_ratio(self) -> None:
        account = uuid.uuid4()
        payments = [_payment(account, WHEN - timedelta(minutes=index)) for index in range(6)]
        values = compute_snapshot(_alert(account), payments).by_name()
        self.assertEqual(values["debit_count_1h"].numeric, Decimal(6))
        self.assertNotIn("debit_hourly_rate_1h_vs_prior_23h", values)

    def test_the_same_inputs_repeat_and_a_changed_amount_does_not(self) -> None:
        account = uuid.uuid4()
        payment = _payment(account, WHEN, amount="12.50")
        first = compute_snapshot(_alert(account), [payment])
        second = compute_snapshot(_alert(account), [payment])
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.values, second.values)
        changed = _payment(account, WHEN, amount="12.51", transaction_id=payment.transaction_id)
        third = compute_snapshot(_alert(account), [changed])
        self.assertNotEqual(first.input_fingerprint, third.input_fingerprint)

    def test_core_names_are_present_for_a_long_history(self) -> None:
        account = uuid.uuid4()
        values = compute_snapshot(_alert(account), [_payment(account, WHEN)]).by_name()
        self.assertTrue(set(CORE_FEATURE_NAMES) <= set(values))
        self.assertEqual(values["availability_basis"].text, "event_timestamp")
        self.assertEqual(values["amount_currency"].text, "USD")

    def test_two_alerts_on_one_account_keep_their_own_cutoff(self) -> None:
        account = uuid.uuid4()
        early = _alert(account, WHEN, alert_id=uuid.uuid4())
        late = _alert(account, WHEN + timedelta(hours=2), alert_id=uuid.uuid4())
        payments = [
            _payment(account, WHEN, amount="10.00"),
            _payment(account, WHEN + timedelta(hours=1), amount="40.00"),
        ]
        early_values = compute_snapshot(early, payments).by_name()
        late_values = compute_snapshot(late, payments).by_name()
        self.assertEqual(early_values["payment_count_24h"].numeric, Decimal(1))
        self.assertEqual(late_values["payment_count_24h"].numeric, Decimal(2))


class FeatureStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = _development_counts(url)
        _recreate_database(url)
        with psycopg.connect(replace_database(url, FEATURE_DATABASE), connect_timeout=5) as connection:
            apply_migrations(connection, default_migrations_dir())
            register_r001(connection)

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not os.environ.get("AMLOG_DATABASE_URL", "").strip():
            return
        _drop_database(cls.url)
        after = _development_counts(cls.url)
        if after != cls.before:
            raise AssertionError(f"The development database changed from {cls.before} to {after}.")

    def test_the_development_database_is_refused(self) -> None:
        with self.assertRaises(FeatureError):
            assert_feature_database("amlog")
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            with self.assertRaises(FeatureError):
                run_feature_snapshots(connection)

    def test_reads_do_not_name_scenario_notes_or_labels(self) -> None:
        combined = _ALERT_QUERY + _PAYMENT_QUERY
        self.assertNotIn("amlog_scenario", combined)
        self.assertNotIn("amlog_label", combined)
        self.assertNotIn("disposition", combined)

    def test_a_snapshot_is_stored_once_and_a_new_input_is_a_new_snapshot(self) -> None:
        account_id, customer_id = self._account(WHEN - timedelta(days=40))
        triggering = self._payment(account_id, customer_id, WHEN, amount="15.00")
        self._payment(account_id, customer_id, WHEN + timedelta(minutes=5), amount="999.00")
        alert_id = self._alert(customer_id, account_id, triggering)
        before_alert = self._alert_stamp(alert_id)
        first = self._run()
        self.assertEqual(first.snapshots_recorded, 1)
        self.assertEqual(first.snapshots_already_present, 0)
        stored = self._features(alert_id)
        self.assertEqual(stored["payment_count_1h"], Decimal(1))
        self.assertEqual(stored["same_currency_debit_sum_24h"], Decimal("15.00000000"))
        self.assertEqual(stored["feature_set_name"], FEATURE_SET_NAME)
        self.assertEqual(stored["feature_set_version"], FEATURE_SET_VERSION)
        self.assertEqual(self._computation_count(alert_id), 1)
        second = self._run()
        self.assertEqual(second.snapshots_recorded, 0)
        self.assertEqual(second.snapshots_already_present, 1)
        self.assertEqual(self._computation_count(alert_id), 1)
        self.assertEqual(self._alert_stamp(alert_id), before_alert)
        self._payment(account_id, customer_id, WHEN - timedelta(hours=2), amount="4.00")
        third = self._run()
        self.assertEqual(third.snapshots_recorded, 1)
        self.assertEqual(self._computation_count(alert_id), 2)
        latest = self._features(alert_id)
        self.assertEqual(latest["payment_count_24h"], Decimal(2))
        self.assertNotEqual(stored["input_fingerprint"], latest["input_fingerprint"])
        self.assertEqual(self._features(alert_id, oldest=True)["payment_count_1h"], Decimal(1))

    def _account(self, opened_at: datetime) -> tuple[uuid.UUID, uuid.UUID]:
        token = uuid.uuid4().hex
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                """
                INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
                VALUES (%s, 'individual', 'Feature test subject')
                RETURNING customer_id
                """,
                (f"feature-{token}",),
            ).fetchone()[0]
            account_id = connection.execute(
                """
                INSERT INTO amlog.accounts (
                    customer_id, account_reference, source_system, source_account_id,
                    account_type, currency_code, opened_at
                )
                VALUES (%s, %s, 'amlog-feature-test', %s, 'deposit', 'USD', %s)
                RETURNING account_id
                """,
                (customer_id, f"feature-account-{token}", token, opened_at),
            ).fetchone()[0]
        return account_id, customer_id

    def _payment(self, account_id: uuid.UUID, customer_id: uuid.UUID, when: datetime, *, amount: str) -> uuid.UUID:
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                INSERT INTO amlog.transactions (
                    source_system, source_event_id, account_id, customer_id,
                    payment_channel, transaction_type, direction, amount, currency_code,
                    event_timestamp, counterparty_reference
                )
                VALUES ('amlog-feature-test', %s, %s, %s, 'card', 'purchase', 'debit', %s, 'USD', %s, 'shop-1')
                RETURNING transaction_id
                """,
                (uuid.uuid4().hex, account_id, customer_id, Decimal(amount), when),
            ).fetchone()[0]

    def _alert(self, customer_id: uuid.UUID, account_id: uuid.UUID, triggering: uuid.UUID) -> uuid.UUID:
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            rule_id = connection.execute(
                "SELECT monitoring_rule_id FROM amlog.monitoring_rules WHERE rule_code = 'R001' AND version_number = 1"
            ).fetchone()[0]
            alert_id = connection.execute(
                """
                INSERT INTO amlog.alerts (
                    alert_reference, monitoring_rule_id, recorded_rule_code, recorded_rule_version,
                    customer_id, account_id, triggered_at, window_started_at, window_ended_at,
                    severity, summary
                )
                VALUES (%s, %s, 'R001', 1, %s, %s, %s, %s, %s, 'medium', 'Feature test alert')
                RETURNING alert_id
                """,
                (
                    f"feature-{uuid.uuid4().hex}",
                    rule_id,
                    customer_id,
                    account_id,
                    WHEN,
                    WHEN - timedelta(hours=1),
                    WHEN,
                ),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO amlog.alert_transactions (alert_id, transaction_id, relationship_role)
                VALUES (%s, %s, 'triggering')
                """,
                (alert_id, triggering),
            )
        return alert_id

    def _run(self):
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            report = run_feature_snapshots(connection)
            connection.commit()
            return report

    def _features(self, alert_id: uuid.UUID, oldest: bool = False) -> dict:
        order = "ASC" if oldest else "DESC"
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            header = connection.execute(
                f"""
                SELECT feature_computation_id, feature_set_name, feature_set_version, input_fingerprint
                FROM amlog.feature_computations
                WHERE alert_id = %s
                ORDER BY computed_at {order}
                LIMIT 1
                """,
                (alert_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT feature_name, value_type, value_numeric, value_text, value_boolean, value_timestamp
                FROM amlog.alert_features
                WHERE feature_computation_id = %s
                """,
                (header[0],),
            ).fetchall()
        found = {
            "feature_set_name": header[1],
            "feature_set_version": header[2],
            "input_fingerprint": header[3],
        }
        for name, value_type, numeric, text, boolean, timestamp in rows:
            if value_type == "numeric":
                found[name] = numeric
            elif value_type == "text":
                found[name] = text
            elif value_type == "boolean":
                found[name] = boolean
            else:
                found[name] = timestamp
        return found

    def _computation_count(self, alert_id: uuid.UUID) -> int:
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                "SELECT count(*) FROM amlog.feature_computations WHERE alert_id = %s",
                (alert_id,),
            ).fetchone()[0]

    def _alert_stamp(self, alert_id: uuid.UUID):
        with psycopg.connect(replace_database(self.url, FEATURE_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                "SELECT updated_at, summary, lifecycle_status FROM amlog.alerts WHERE alert_id = %s",
                (alert_id,),
            ).fetchone()


def _development_counts(url: str) -> tuple[int, int, int, int]:
    with psycopg.connect(url, connect_timeout=5) as connection:
        current = connection.execute("SELECT current_database()").fetchone()[0]
        if current != "amlog":
            raise AssertionError(f"AMLOG_DATABASE_URL points at {current}, not amlog.")
        transactions = connection.execute("SELECT count(*) FROM amlog.transactions").fetchone()[0]
        alerts = connection.execute("SELECT count(*) FROM amlog.alerts").fetchone()[0]
        features = connection.execute("SELECT count(*) FROM amlog.feature_computations").fetchone()[0]
        rules = connection.execute("SELECT count(*) FROM amlog.monitoring_rules").fetchone()[0]
    return int(transactions), int(alerts), int(features), int(rules)


def _recreate_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(FEATURE_DATABASE)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(FEATURE_DATABASE)))
    finally:
        admin.close()


def _drop_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(FEATURE_DATABASE)))
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

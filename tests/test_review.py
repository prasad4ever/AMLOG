"""Investigator review persistence.

Writes go to the disposable database amlog_review_test. The development
database and the existing workload databases are counted before and after
and must not change.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("AMLOG_ENVIRONMENT", "test")

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover
    psycopg = None

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from amlog.config import default_migrations_dir  # noqa: E402
from amlog.db.migrate import apply_migrations  # noqa: E402
from amlog.monitor.engine import register_r001  # noqa: E402
from amlog.review.server import ReviewServer  # noqa: E402
from amlog.review.service import (  # noqa: E402
    ReviewError,
    add_finding,
    assert_review_database,
    close_case,
    get_alert,
    open_case,
    readiness,
    record_assessment,
    reopen_case,
)
from amlog.review.serve import main as serve_main  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402

REVIEW_DATABASE = "amlog_review_test"
WHEN = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
PRESERVED = (
    "amlog",
    "amlog_workload",
    "amlog_workload_r002_validation",
    "amlog_workload_integrated",
)


class ReviewServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = {name: _counts(url, name) for name in PRESERVED}
        _recreate_database(url)
        with psycopg.connect(replace_database(url, REVIEW_DATABASE), connect_timeout=5) as connection:
            apply_migrations(connection, default_migrations_dir())
            register_r001(connection)

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not os.environ.get("AMLOG_DATABASE_URL", "").strip():
            return
        _drop_database(cls.url)
        after = {name: _counts(cls.url, name) for name in PRESERVED}
        if after != cls.before:
            raise AssertionError(f"A preserved database changed from {cls.before} to {after}.")

    def test_the_development_database_is_refused(self) -> None:
        with self.assertRaises(ReviewError):
            assert_review_database("amlog")
        self.assertEqual(serve_main(["--database", "amlog", "--port", "8765"]), 2)
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            with self.assertRaises(ReviewError):
                open_case(connection, uuid.uuid4(), "reviewer-a")

    def test_review_code_does_not_read_scenario_notes(self) -> None:
        root = SRC / "amlog" / "review"
        combined = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        self.assertNotIn("amlog_scenario", combined)

    def test_a_review_is_saved_retrieved_and_not_duplicated(self) -> None:
        left = self._party("left")
        right = self._party("right")
        own = self._payment(left, WHEN, amount="20.00")
        other = self._payment(right, WHEN, amount="900.00")
        alert_id = self._alert(left, own, other_transaction=other)
        before = self._snapshot(alert_id)
        first = self._call(open_case, alert_id, "reviewer-a")
        second = self._call(open_case, alert_id, "reviewer-b")
        self.assertEqual(first, second)
        self._call(add_finding, alert_id, "reviewer-a", "The six card payments are ordinary retail amounts.")
        label_id = self._call(
            record_assessment,
            alert_id,
            "reviewer-a",
            "productive",
            "The burst was worth a person looking at.",
        )
        with self.assertRaises(ReviewError):
            self._call(
                record_assessment,
                alert_id,
                "reviewer-a",
                "productive",
                "The burst was worth a person looking at.",
            )
        detail = self._call(get_alert, alert_id)
        self.assertEqual(detail.case.investigation_case_id, first)
        self.assertEqual(detail.case.lifecycle_status, "in_progress")
        self.assertIsNone(detail.case.disposition)
        self.assertEqual(len(detail.case.notes), 2)
        self.assertEqual(detail.assessments[-1].label_value, "productive_alert")
        self.assertFalse(detail.assessments[-1].eligible_for_evaluation)
        self.assertEqual(detail.assessments[-1].data_origin, "operational")
        self.assertEqual(tuple(payment.transaction_id for payment in detail.payments), (own,))
        self.assertNotIn(other, {payment.transaction_id for payment in detail.payments})
        after = self._snapshot(alert_id)
        self.assertEqual(after[:5], before[:5])
        self.assertEqual(after[6], "open")
        self.assertEqual(self._label_count(alert_id), 1)
        self.assertEqual(self._case_count(left[0]), 1)
        stored = self._label(label_id)
        self.assertEqual(stored[0], "investigator_outcome")
        self.assertEqual(stored[1], "operational")
        self.assertFalse(stored[2])

    def test_status_changes_keep_audit_history_and_reject_conflicts(self) -> None:
        party = self._party("status")
        payment = self._payment(party, WHEN, amount="12.00")
        alert_id = self._alert(party, payment)
        self._call(open_case, alert_id, "reviewer-a")
        with self.assertRaises(ReviewError):
            self._call(close_case, alert_id, "reviewer-a", "escalated", "Closing without a usefulness judgment.")
        self._call(record_assessment, alert_id, "reviewer-a", "non_productive", "Retail pattern, no further review value.")
        self._call(close_case, alert_id, "reviewer-a", "no_further_action", "No operational follow-up.")
        with self.assertRaises(ReviewError):
            self._call(record_assessment, alert_id, "reviewer-a", "productive", "Changed view after close.")
        self._call(reopen_case, alert_id, "reviewer-b", "A second reviewer wants another look.")
        self._call(record_assessment, alert_id, "reviewer-b", "inconclusive", "The second look did not settle usefulness.")
        detail = self._call(get_alert, alert_id)
        self.assertEqual(detail.case.lifecycle_status, "in_progress")
        self.assertIsNone(detail.case.disposition)
        self.assertEqual(
            [item.label_value for item in detail.assessments],
            ["non_productive_alert", "uncertain"],
        )
        self.assertTrue(all(item.eligible_for_evaluation is False for item in detail.assessments))
        actions = self._actions(alert_id, detail.case.investigation_case_id)
        self.assertIn("case_opened", actions)
        self.assertIn("case_closed", actions)
        self.assertIn("case_reopened", actions)
        reopened = self._audit("case_reopened", detail.case.investigation_case_id)
        self.assertEqual(reopened["previous"]["disposition"], "no_further_action")
        self.assertEqual(reopened["actor_type"], "person")
        self.assertEqual(reopened["actor"], "reviewer-b")
        report = self._call(readiness)
        self.assertEqual(report.usable_training_outcomes, 0)
        self.assertGreaterEqual(report.investigator_assessments, 2)

    def test_a_case_for_another_customer_cannot_be_linked(self) -> None:
        left = self._party("owner")
        right = self._party("other-owner")
        payment = self._payment(left, WHEN, amount="8.00")
        alert_id = self._alert(left, payment)
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            case_id = connection.execute(
                """
                INSERT INTO amlog.investigation_cases (
                    case_reference, customer_id, opened_at
                )
                VALUES (%s, %s, %s)
                RETURNING investigation_case_id
                """,
                (f"INV-OTHER-{uuid.uuid4()}", right[0], WHEN),
            ).fetchone()[0]
            with self.assertRaises(psycopg.Error):
                connection.execute(
                    "UPDATE amlog.alerts SET investigation_case_id = %s WHERE alert_id = %s",
                    (case_id, alert_id),
                )

    def test_the_local_page_saves_and_shows_a_decision(self) -> None:
        party = self._party("page")
        payment = self._payment(party, WHEN, amount="11.00")
        alert_id = self._alert(party, payment)
        url = replace_database(self.url, REVIEW_DATABASE)
        server = ReviewServer(("127.0.0.1", 0), url)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            listing = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
            self.assertIn(party[2], listing)
            self.assertIn("Usable training outcomes: 0", listing)
            before = urllib.request.urlopen(base + f"/alert?id={alert_id}", timeout=5).read().decode("utf-8")
            self.assertIn("No investigation is open", before)
            body = urllib.parse.urlencode(
                {"alert_id": str(alert_id), "reviewer": "reviewer-page"}
            ).encode("utf-8")
            page = urllib.request.urlopen(
                urllib.request.Request(base + "/open", data=body),
                timeout=5,
            ).read().decode("utf-8")
            self.assertIn(f"INV-{alert_id}", page)
            self.assertNotIn("No investigation is open", page)
        finally:
            server.shutdown()
            server.server_close()

    def _party(self, name: str) -> tuple[uuid.UUID, uuid.UUID, str]:
        token = uuid.uuid4().hex
        reference = f"review-{name}-{token}"
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                """
                INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
                VALUES (%s, 'individual', %s)
                RETURNING customer_id
                """,
                (reference, f"Review subject {name}"),
            ).fetchone()[0]
            account_id = connection.execute(
                """
                INSERT INTO amlog.accounts (
                    customer_id, account_reference, source_system, source_account_id,
                    account_type, currency_code, opened_at
                )
                VALUES (%s, %s, 'amlog-review-test', %s, 'deposit', 'USD', %s)
                RETURNING account_id
                """,
                (customer_id, f"account-{token}", token, WHEN - timedelta(days=40)),
            ).fetchone()[0]
        return customer_id, account_id, reference

    def _payment(self, party: tuple[uuid.UUID, uuid.UUID, str], when: datetime, *, amount: str) -> uuid.UUID:
        customer_id, account_id, _reference = party
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                INSERT INTO amlog.transactions (
                    source_system, source_event_id, account_id, customer_id,
                    payment_channel, transaction_type, direction, amount, currency_code,
                    event_timestamp, counterparty_reference
                )
                VALUES ('amlog-review-test', %s, %s, %s, 'card', 'purchase', 'debit', %s, 'USD', %s, 'shop-1')
                RETURNING transaction_id
                """,
                (uuid.uuid4().hex, account_id, customer_id, Decimal(amount), when),
            ).fetchone()[0]

    def _alert(
        self,
        party: tuple[uuid.UUID, uuid.UUID, str],
        triggering: uuid.UUID,
        other_transaction: uuid.UUID | None = None,
    ) -> uuid.UUID:
        customer_id, account_id, _reference = party
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
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
                VALUES (%s, %s, 'R001', 1, %s, %s, %s, %s, %s, 'medium', 'Review test alert')
                RETURNING alert_id
                """,
                (
                    f"review-{uuid.uuid4().hex}",
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
            if other_transaction is not None:
                connection.execute(
                    """
                    INSERT INTO amlog.alert_transactions (alert_id, transaction_id, relationship_role)
                    VALUES (%s, %s, 'supporting')
                    """,
                    (alert_id, other_transaction),
                )
        return alert_id

    def _call(self, function, *args):
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return function(connection, *args)

    def _snapshot(self, alert_id: uuid.UUID):
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT recorded_rule_code, recorded_rule_version, summary, triggered_at,
                       account_id, investigation_case_id, lifecycle_status
                FROM amlog.alerts
                WHERE alert_id = %s
                """,
                (alert_id,),
            ).fetchone()

    def _label_count(self, alert_id: uuid.UUID) -> int:
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                "SELECT count(*) FROM amlog_label.alert_labels WHERE alert_id = %s",
                (alert_id,),
            ).fetchone()[0]

    def _label(self, label_id: uuid.UUID):
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT label_source, data_origin, eligible_for_evaluation
                FROM amlog_label.alert_labels
                WHERE alert_label_id = %s
                """,
                (label_id,),
            ).fetchone()

    def _case_count(self, customer_id: uuid.UUID) -> int:
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                "SELECT count(*) FROM amlog.investigation_cases WHERE customer_id = %s",
                (customer_id,),
            ).fetchone()[0]

    def _actions(self, alert_id: uuid.UUID, case_id: uuid.UUID) -> list[str]:
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            rows = connection.execute(
                """
                SELECT action
                FROM amlog.audit_events
                WHERE entity_id = %s
                   OR context_document ->> 'alert_id' = %s
                ORDER BY occurred_at
                """,
                (case_id, str(alert_id)),
            ).fetchall()
        return [row[0] for row in rows]

    def _audit(self, action: str, case_id: uuid.UUID) -> dict:
        with psycopg.connect(replace_database(self.url, REVIEW_DATABASE), connect_timeout=5) as connection:
            row = connection.execute(
                """
                SELECT actor_type, actor_reference, previous_document
                FROM amlog.audit_events
                WHERE action = %s AND entity_id = %s
                """,
                (action, case_id),
            ).fetchone()
        previous = row[2]
        if isinstance(previous, str):
            previous = json.loads(previous)
        return {"actor_type": row[0], "actor": row[1], "previous": previous}


def _counts(url: str, database_name: str) -> tuple[int, ...] | None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        ).fetchone()
    finally:
        admin.close()
    if exists is None:
        return None
    with psycopg.connect(replace_database(url, database_name), connect_timeout=5) as connection:
        if database_name == "amlog":
            current = connection.execute("SELECT current_database()").fetchone()[0]
            if current != "amlog":
                raise AssertionError(f"AMLOG_DATABASE_URL points at {current}, not amlog.")
        return tuple(
            int(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(*table.split(".")))
                ).fetchone()[0]
            )
            for table in (
                "amlog.transactions",
                "amlog.alerts",
                "amlog.investigation_cases",
                "amlog_label.alert_labels",
            )
        )


def _recreate_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(REVIEW_DATABASE)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(REVIEW_DATABASE)))
    finally:
        admin.close()


def _drop_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(REVIEW_DATABASE)))
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

"""PostgreSQL tests for transaction ingestion.

Each test opens one transaction and rolls it back. The development
demonstration payment is not used here, and these rows must not remain.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import psycopg
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None

from amlog.ingest.contract import EVENT_TYPE  # noqa: E402
from amlog.ingest.service import TransactionIngestor  # noqa: E402


def database_url() -> str:
    return os.environ.get("AMLOG_DATABASE_URL", "").strip()


class TransactionIngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = database_url()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url

    def setUp(self) -> None:
        self.conn = psycopg.connect(self.url, connect_timeout=5)
        self.conn.autocommit = False
        # Keep an outer transaction open so ingestion uses a savepoint.
        # The savepoint is released on success. tearDown rolls the outer
        # transaction back, which removes every fixture row.
        self.conn.execute("SELECT 1")
        self.ingestor = TransactionIngestor(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.rollback()
        finally:
            self.conn.close()

    def test_valid_event_is_stored_and_can_be_read_back(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)
        payload = self._payload(account_reference)
        result = self.ingestor.ingest(payload)

        self.assertEqual(result.outcome, "recorded")
        self.assertEqual(result.customer_id, customer_id)
        self.assertIsNotNone(result.transaction_id)
        self.assertIsNotNone(result.audit_event_id)

        row = self.conn.execute(
            """
            SELECT
                customer_id,
                account_id,
                direction,
                amount,
                btrim(currency_code::text),
                base_amount,
                fx_rate,
                event_timestamp,
                booked_at,
                ingested_at,
                corrects_transaction_id,
                payment_channel,
                transaction_type
            FROM amlog.transactions
            WHERE transaction_id = %s
            """,
            (result.transaction_id,),
        ).fetchone()
        self.assertEqual(row[0], customer_id)
        self.assertEqual(row[1], result.account_id)
        self.assertEqual(row[2], "debit")
        self.assertEqual(row[3], Decimal("10.25"))
        self.assertEqual(row[4], "EUR")
        self.assertEqual(row[5], Decimal("11.10"))
        self.assertEqual(row[6], Decimal("1.08292683"))
        self.assertEqual(row[7], datetime(2026, 3, 1, 15, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(row[8], datetime(2026, 3, 1, 18, 30, tzinfo=timezone.utc))
        self.assertIsNotNone(row[9])
        self.assertIsNone(row[10])
        self.assertEqual(row[11], "wire")
        self.assertEqual(row[12], "transfer")

        audit_amount = self.conn.execute(
            """
            SELECT resulting_document->>'amount'
            FROM amlog.audit_events
            WHERE audit_event_id = %s
            """,
            (result.audit_event_id,),
        ).fetchone()[0]
        self.assertEqual(audit_amount, "10.25")
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 1)

    def test_customer_comes_from_the_account_owner(self) -> None:
        owner_id = self._insert_customer()
        other_id = self._insert_customer()
        account_reference = self._insert_account(owner_id)
        result = self.ingestor.ingest(self._payload(account_reference))

        self.assertEqual(result.outcome, "recorded")
        self.assertEqual(result.customer_id, owner_id)
        self.assertNotEqual(result.customer_id, other_id)

    def test_supplied_customer_is_rejected_and_nothing_is_stored(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)
        payload = self._payload(account_reference)
        payload["customer_id"] = str(uuid.uuid4())
        result = self.ingestor.ingest(payload)

        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.reason, "invalid_event")
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 0)

    def test_unknown_account_is_rejected(self) -> None:
        payload = self._payload(f"missing-{uuid.uuid4().hex}")
        before = self._table_count("amlog.transactions")
        result = self.ingestor.ingest(payload)

        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.reason, "account_not_found")
        self.assertIn("No account is registered", result.message)
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 0)
        self.assertEqual(self._table_count("amlog.transactions"), before)

    def test_identical_replay_does_not_create_another_transaction(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)
        payload = self._payload(account_reference)
        first = self.ingestor.ingest(payload)
        second = self.ingestor.ingest(dict(payload))

        self.assertEqual(first.outcome, "recorded")
        self.assertEqual(second.outcome, "already_recorded")
        self.assertEqual(second.reason, "identical_replay")
        self.assertEqual(second.transaction_id, first.transaction_id)
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 1)
        self.assertEqual(self._audit_count(first.transaction_id), 1)

    def test_same_identity_with_different_details_conflicts(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)
        payload = self._payload(account_reference)
        first = self.ingestor.ingest(payload)

        changed = dict(payload)
        changed["amount"] = "99.00"
        conflict = self.ingestor.ingest(changed)

        self.assertEqual(conflict.outcome, "rejected")
        self.assertEqual(conflict.reason, "idempotency_conflict")
        self.assertEqual(conflict.conflicting_fields, ("amount",))
        self.assertEqual(conflict.transaction_id, first.transaction_id)
        amount = self.conn.execute(
            "SELECT amount FROM amlog.transactions WHERE transaction_id = %s",
            (first.transaction_id,),
        ).fetchone()[0]
        self.assertEqual(amount, Decimal("10.25"))
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 1)
        self.assertEqual(self._audit_count(first.transaction_id), 1)

    def test_same_identity_for_a_different_account_conflicts(self) -> None:
        first_owner = self._insert_customer()
        second_owner = self._insert_customer()
        first_account = self._insert_account(first_owner)
        second_account = self._insert_account(second_owner)
        payload = self._payload(first_account)
        first = self.ingestor.ingest(payload)

        moved = dict(payload)
        moved["account_reference"] = second_account
        conflict = self.ingestor.ingest(moved)

        self.assertEqual(conflict.reason, "idempotency_conflict")
        self.assertIn("account_id", conflict.conflicting_fields)
        self.assertEqual(conflict.customer_id, first_owner)
        self.assertNotEqual(conflict.customer_id, second_owner)
        self.assertEqual(conflict.transaction_id, first.transaction_id)

    def test_invalid_amount_and_naive_timestamp_store_nothing(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)

        amount_payload = self._payload(account_reference)
        amount_payload["amount"] = 10.25
        amount_result = self.ingestor.ingest(amount_payload)
        self.assertEqual(amount_result.reason, "invalid_event")
        self.assertEqual(self._transaction_count(amount_payload["source_event_id"]), 0)

        time_payload = self._payload(account_reference)
        time_payload["event_timestamp"] = "2026-03-01T15:04:05"
        time_result = self.ingestor.ingest(time_payload)
        self.assertEqual(time_result.reason, "invalid_event")
        self.assertIn("time zone", time_result.message)
        self.assertEqual(self._transaction_count(time_payload["source_event_id"]), 0)

    def test_uncommitted_ingestion_is_invisible_to_another_connection(self) -> None:
        customer_id = self._insert_customer()
        account_reference = self._insert_account(customer_id)
        payload = self._payload(account_reference)
        result = self.ingestor.ingest(payload)
        self.assertEqual(result.outcome, "recorded")

        other = psycopg.connect(self.url, connect_timeout=5)
        other.autocommit = True
        try:
            visible = other.execute(
                """
                SELECT transaction_id
                FROM amlog.transactions
                WHERE source_system = %s AND source_event_id = %s
                """,
                (payload["source_system"], payload["source_event_id"]),
            ).fetchone()
        finally:
            other.close()
        self.assertIsNone(visible)
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 1)

    def test_three_payment_sources_resolve_independently(self) -> None:
        shared_account_id = f"EXTERNAL-{uuid.uuid4().hex}"
        shared_event_id = f"event-{uuid.uuid4().hex}"
        sources = (
            ("wallet", "wallet", "credit", "20.00"),
            ("bank-transfer", "bank-transfer", "debit", "75.00"),
            ("merchant-payment", "card", "debit", "8.40"),
        )
        accounts = {}
        first_payloads = {}
        for source_system, channel, direction, amount in sources:
            customer_id = self._insert_customer()
            account = self._insert_sourced_account(customer_id, source_system, shared_account_id)
            accounts[source_system] = account
            payload = self._source_payload(
                source_system,
                shared_account_id,
                source_event_id=shared_event_id,
                payment_channel=channel,
                direction=direction,
                amount=amount,
            )
            first_payloads[source_system] = payload
            result = self.ingestor.ingest(payload)
            self.assertEqual(result.outcome, "recorded", result.message)
            self.assertEqual(result.customer_id, customer_id)
            self.assertEqual(result.account_id, account["account_id"])
            self.assertNotIn("customer_id", payload)

        self.assertEqual(len({item["customer_id"] for item in accounts.values()}), 3)
        wallet_id = accounts["wallet"]["customer_id"]
        bank_id = accounts["bank-transfer"]["customer_id"]
        merchant_id = accounts["merchant-payment"]["customer_id"]
        self.assertNotEqual(wallet_id, bank_id)
        self.assertNotEqual(wallet_id, merchant_id)

        wallet_replay = dict(first_payloads["wallet"])
        replay = self.ingestor.ingest(wallet_replay)
        self.assertEqual(replay.outcome, "already_recorded")
        self.assertEqual(replay.customer_id, wallet_id)

        conflict = dict(wallet_replay)
        conflict["amount"] = "21.00"
        rejected = self.ingestor.ingest(conflict)
        self.assertEqual(rejected.reason, "idempotency_conflict")
        self.assertIn("amount", rejected.conflicting_fields)
        wallet_amount = self.conn.execute(
            """
            SELECT amount
            FROM amlog.transactions
            WHERE source_system = 'wallet' AND source_event_id = %s
            """,
            (shared_event_id,),
        ).fetchone()[0]
        self.assertEqual(wallet_amount, Decimal("20.00"))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM amlog.transactions WHERE source_event_id = %s",
                (shared_event_id,),
            ).fetchone()[0],
            3,
        )
        self.assertEqual(self._audit_count(replay.transaction_id), 1)

        other = psycopg.connect(self.url, connect_timeout=5)
        other.autocommit = True
        try:
            visible = other.execute(
                "SELECT count(*) FROM amlog.transactions WHERE source_event_id = %s",
                (shared_event_id,),
            ).fetchone()[0]
        finally:
            other.close()
        self.assertEqual(visible, 0)

    def test_unknown_source_account_does_not_create_a_customer_or_account(self) -> None:
        before = self._population_counts()
        payload = self._source_payload("wallet", f"missing-{uuid.uuid4().hex}")
        result = self.ingestor.ingest(payload)

        self.assertEqual(result.reason, "account_not_found")
        self.assertIn("no customer or account was created", result.message)
        self.assertEqual(self._population_counts(), before)
        self.assertEqual(self._transaction_count(payload["source_event_id"]), 0)
        registered = self.conn.execute(
            """
            SELECT count(*)
            FROM amlog.accounts
            WHERE source_system = %s AND source_account_id = %s
            """,
            (payload["source_system"], payload["source_account_id"]),
        ).fetchone()[0]
        self.assertEqual(registered, 0)

    def test_canonical_and_source_references_must_name_the_same_account(self) -> None:
        source_account_id = f"EXTERNAL-{uuid.uuid4().hex}"
        wallet_customer = self._insert_customer()
        bank_customer = self._insert_customer()
        wallet = self._insert_sourced_account(wallet_customer, "wallet", source_account_id)
        bank = self._insert_sourced_account(bank_customer, "bank-transfer", source_account_id)

        mismatched = self._source_payload("wallet", source_account_id)
        mismatched["account_reference"] = bank["account_reference"]
        before = self._population_counts()
        mismatch_result = self.ingestor.ingest(mismatched)
        self.assertEqual(mismatch_result.reason, "inconsistent_account_reference")
        self.assertIn("different accounts", mismatch_result.message)
        self.assertEqual(self._population_counts(), before)

        missing_source = self._source_payload("wallet", f"missing-{uuid.uuid4().hex}")
        missing_source["account_reference"] = wallet["account_reference"]
        missing_source_result = self.ingestor.ingest(missing_source)
        self.assertEqual(missing_source_result.reason, "inconsistent_account_reference")
        self.assertIn("not used in its place", missing_source_result.message)
        self.assertEqual(self._transaction_count(missing_source["source_event_id"]), 0)

        agreed = self._source_payload("wallet", source_account_id)
        agreed["account_reference"] = wallet["account_reference"]
        agreed_result = self.ingestor.ingest(agreed)
        self.assertEqual(agreed_result.outcome, "recorded")
        self.assertEqual(agreed_result.account_id, wallet["account_id"])
        self.assertEqual(agreed_result.customer_id, wallet_customer)
        self.assertNotEqual(agreed_result.customer_id, bank_customer)

    def test_replay_by_canonical_reference_matches_a_source_account_delivery(self) -> None:
        source_account_id = f"EXTERNAL-{uuid.uuid4().hex}"
        customer_id = self._insert_customer()
        account = self._insert_sourced_account(customer_id, "merchant-payment", source_account_id)
        payload = self._source_payload(
            "merchant-payment",
            source_account_id,
            payment_channel="card",
            direction="debit",
            amount="8.40",
        )
        first = self.ingestor.ingest(payload)
        self.assertEqual(first.outcome, "recorded")

        replay = dict(payload)
        replay["schema_version"] = "1"
        del replay["source_account_id"]
        replay["account_reference"] = account["account_reference"]
        second = self.ingestor.ingest(replay)
        self.assertEqual(second.outcome, "already_recorded")
        self.assertEqual(second.transaction_id, first.transaction_id)
        self.assertEqual(self._audit_count(first.transaction_id), 1)
        stored_rows = self.conn.execute(
            "SELECT count(*) FROM amlog.transactions WHERE transaction_id = %s",
            (first.transaction_id,),
        ).fetchone()[0]
        self.assertEqual(stored_rows, 1)

    def _source_payload(self, source_system: str, source_account_id: str, **overrides: object) -> dict:
        payload = {
            "schema_version": "2",
            "event_type": EVENT_TYPE,
            "source_system": source_system,
            "source_event_id": uuid.uuid4().hex,
            "source_account_id": source_account_id,
            "transaction_type": "transfer",
            "payment_channel": source_system,
            "direction": "debit",
            "amount": "10.00",
            "currency_code": "USD",
            "event_timestamp": "2026-04-01T12:00:00+00:00",
            "correlation_id": str(uuid.uuid4()),
        }
        payload.update(overrides)
        return payload

    def _insert_sourced_account(
        self,
        customer_id: uuid.UUID,
        source_system: str,
        source_account_id: str,
    ) -> dict:
        reference = f"{source_system}-{uuid.uuid4().hex}"
        row = self.conn.execute(
            """
            INSERT INTO amlog.accounts (
                customer_id,
                account_reference,
                source_system,
                source_account_id,
                account_type,
                currency_code
            )
            VALUES (%s, %s, %s, %s, 'deposit', 'USD')
            RETURNING account_id
            """,
            (customer_id, reference, source_system, source_account_id),
        ).fetchone()
        return {
            "account_id": row[0],
            "customer_id": customer_id,
            "account_reference": reference,
            "source_system": source_system,
            "source_account_id": source_account_id,
        }

    def _population_counts(self) -> dict[str, int]:
        counts = {}
        for name in ("customers", "accounts", "transactions", "audit_events"):
            counts[name] = self.conn.execute(f"SELECT count(*) FROM amlog.{name}").fetchone()[0]
        return counts

    def _payload(self, account_reference: str) -> dict:
        return {
            "schema_version": "1",
            "event_type": EVENT_TYPE,
            "source_system": "amlog-ingest-test",
            "source_event_id": uuid.uuid4().hex,
            "account_reference": account_reference,
            "transaction_type": "transfer",
            "payment_channel": "wire",
            "direction": "debit",
            "amount": "10.25",
            "currency_code": "EUR",
            "event_timestamp": "2026-03-01T15:04:05+00:00",
            "booked_at": "2026-03-01T16:00:00-02:30",
            "base_currency_code": "USD",
            "base_amount": "11.10",
            "fx_rate": "1.08292683",
            "counterparty_reference": "cp-test",
            "counterparty_name": "Ingest test counterparty",
            "counterparty_country_code": "CA",
            "correlation_id": str(uuid.uuid4()),
        }

    def _insert_customer(self) -> uuid.UUID:
        row = self.conn.execute(
            """
            INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
            VALUES (%s, 'organization', 'Ingest test subject')
            RETURNING customer_id
            """,
            (f"ingest-test-{uuid.uuid4().hex}",),
        ).fetchone()
        return row[0]

    def _insert_account(self, customer_id: uuid.UUID) -> str:
        token = uuid.uuid4().hex
        reference = f"ingest-account-{token}"
        self.conn.execute(
            """
            INSERT INTO amlog.accounts (
                customer_id,
                account_reference,
                source_system,
                source_account_id,
                account_type,
                currency_code
            )
            VALUES (%s, %s, 'amlog-ingest-test', %s, 'deposit', 'USD')
            """,
            (customer_id, reference, token),
        )
        return reference

    def _transaction_count(self, source_event_id: str) -> int:
        return self.conn.execute(
            """
            SELECT count(*)
            FROM amlog.transactions
            WHERE source_system = 'amlog-ingest-test'
              AND source_event_id = %s
            """,
            (source_event_id,),
        ).fetchone()[0]

    def _audit_count(self, transaction_id: uuid.UUID) -> int:
        return self.conn.execute(
            """
            SELECT count(*)
            FROM amlog.audit_events
            WHERE entity_schema = 'amlog'
              AND entity_table = 'transactions'
              AND entity_id = %s
            """,
            (transaction_id,),
        ).fetchone()[0]

    def _table_count(self, table_name: str) -> int:
        if table_name != "amlog.transactions":
            raise AssertionError(table_name)
        return self.conn.execute("SELECT count(*) FROM amlog.transactions").fetchone()[0]


if __name__ == "__main__":
    unittest.main()

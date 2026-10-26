"""Validation tests for the transaction event contract.

These tests do not connect to PostgreSQL.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.ingest.contract import (  # noqa: E402
    EVENT_TYPE,
    EventValidationError,
    parse_transaction_event,
)
from amlog.ingest.demo import DEMONSTRATION_EVENT, SOURCE_SYSTEM  # noqa: E402
from amlog.ingest.service import TransactionIngestor, differing_fields  # noqa: E402


def valid_payload() -> dict:
    return {
        "schema_version": "1",
        "event_type": EVENT_TYPE,
        "source_system": "core-payments",
        "source_event_id": "evt-100",
        "account_reference": "ACCT-100",
        "transaction_type": "transfer",
        "payment_channel": "wire",
        "direction": "credit",
        "amount": "10.25",
        "currency_code": "USD",
        "event_timestamp": "2026-03-01T15:04:05+00:00",
        "counterparty_reference": "CP-9",
        "correlation_id": "6f1c1a4e-8a0d-4c3e-9b2a-0d5e7c8b9a11",
    }


class ExplodingConnection:
    def execute(self, *args, **kwargs):
        raise AssertionError("an invalid event must not query the database")

    def transaction(self):
        raise AssertionError("an invalid event must not open a database transaction")


class TransactionContractTests(unittest.TestCase):
    def test_valid_event_uses_decimal_and_a_timezone(self) -> None:
        event = parse_transaction_event(valid_payload())

        self.assertEqual(event.schema_version, "1")
        self.assertEqual(event.event_type, EVENT_TYPE)
        self.assertEqual(event.amount, Decimal("10.25"))
        self.assertEqual(event.direction, "credit")
        self.assertEqual(event.event_timestamp, datetime(2026, 3, 1, 15, 4, 5, tzinfo=timezone.utc))
        self.assertIsNotNone(event.event_timestamp.tzinfo)
        self.assertEqual(event.correlation_id, uuid.UUID("6f1c1a4e-8a0d-4c3e-9b2a-0d5e7c8b9a11"))

    def test_integer_schema_version_and_integer_amount_are_exact(self) -> None:
        payload = valid_payload()
        payload["schema_version"] = 1
        payload["amount"] = 10
        event = parse_transaction_event(payload)
        self.assertEqual(event.amount, Decimal("10"))

    def test_zero_amount_is_valid(self) -> None:
        payload = valid_payload()
        payload["amount"] = "0.00"
        self.assertEqual(parse_transaction_event(payload).amount, Decimal("0.00"))

    def test_zulu_timestamp_is_utc(self) -> None:
        payload = valid_payload()
        payload["event_timestamp"] = "2026-03-01T15:04:05Z"
        event = parse_transaction_event(payload)
        self.assertEqual(event.event_timestamp, datetime(2026, 3, 1, 15, 4, 5, tzinfo=timezone.utc))

    def test_offset_timestamp_is_converted_to_utc(self) -> None:
        payload = valid_payload()
        payload["event_timestamp"] = "2026-03-01T12:34:05-02:30"
        event = parse_transaction_event(payload)
        self.assertEqual(event.event_timestamp, datetime(2026, 3, 1, 15, 4, 5, tzinfo=timezone.utc))

    def test_float_amount_is_rejected_without_touching_a_database(self) -> None:
        payload = valid_payload()
        payload["amount"] = 10.25
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        self.assertIn("floating-point", caught.exception.errors[0].message)

        result = TransactionIngestor(ExplodingConnection()).ingest(payload)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.reason, "invalid_event")
        self.assertFalse(result.stored)

    def test_boolean_amount_and_boolean_schema_version_are_rejected(self) -> None:
        payload = valid_payload()
        payload["amount"] = True
        payload["schema_version"] = True
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        fields = {error.field for error in caught.exception.errors}
        self.assertIn("amount", fields)
        self.assertIn("schema_version", fields)

    def test_negative_amount_and_excess_precision_are_rejected(self) -> None:
        negative = valid_payload()
        negative["amount"] = "-0.01"
        with self.assertRaises(EventValidationError) as negative_error:
            parse_transaction_event(negative)
        self.assertIn("Direction carries the sign", negative_error.exception.errors[0].message)

        precise = valid_payload()
        precise["amount"] = "1.123456789"
        with self.assertRaises(EventValidationError) as precise_error:
            parse_transaction_event(precise)
        self.assertIn("8", precise_error.exception.errors[0].message)

        too_wide = valid_payload()
        too_wide["amount"] = "1" + ("0" * 30)
        with self.assertRaises(EventValidationError):
            parse_transaction_event(too_wide)

    def test_spaced_decimal_string_is_rejected(self) -> None:
        payload = valid_payload()
        payload["amount"] = " 10.25"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        self.assertEqual(caught.exception.errors[0].field, "amount")

    def test_naive_and_date_only_timestamps_are_rejected(self) -> None:
        naive = valid_payload()
        naive["event_timestamp"] = datetime(2026, 3, 1, 15, 4, 5)
        with self.assertRaises(EventValidationError) as naive_error:
            parse_transaction_event(naive)
        self.assertIn("time zone", naive_error.exception.errors[0].message)

        date_only = valid_payload()
        date_only["event_timestamp"] = "2026-03-01"
        with self.assertRaises(EventValidationError) as date_error:
            parse_transaction_event(date_only)
        self.assertEqual(date_error.exception.errors[0].field, "event_timestamp")

        missing_offset = valid_payload()
        missing_offset["event_timestamp"] = "2026-03-01T15:04:05"
        with self.assertRaises(EventValidationError):
            parse_transaction_event(missing_offset)

    def test_direction_and_currency_must_match_the_stored_forms(self) -> None:
        payload = valid_payload()
        payload["direction"] = "Debit"
        payload["currency_code"] = "usd"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        fields = {error.field for error in caught.exception.errors}
        self.assertEqual(fields, {"direction", "currency_code"})

    def test_unknown_schema_version_is_rejected(self) -> None:
        payload = valid_payload()
        payload["schema_version"] = "3"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        self.assertIn("not supported", caught.exception.errors[0].message)

    def test_customer_label_and_score_fields_are_rejected(self) -> None:
        payload = valid_payload()
        payload["customer_id"] = str(uuid.uuid4())
        payload["label_value"] = "productive_alert"
        payload["is_fraud"] = False
        payload["risk_score"] = "0.2"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        messages = " ".join(error.message for error in caught.exception.errors)
        self.assertIn("Customer ownership", messages)
        self.assertIn("Training labels", messages)
        self.assertIn("Fraud judgments", messages)
        self.assertIn("Risk scores", messages)

    def test_unknown_field_is_rejected(self) -> None:
        payload = valid_payload()
        payload["narrative"] = "free text"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        self.assertEqual(caught.exception.errors[0].field, "narrative")

    def test_base_amount_and_exchange_rate_must_be_a_complete_pair(self) -> None:
        currency_only = valid_payload()
        currency_only["base_currency_code"] = "EUR"
        with self.assertRaises(EventValidationError):
            parse_transaction_event(currency_only)

        rate_only = valid_payload()
        rate_only["fx_rate"] = "1.10"
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(rate_only)
        self.assertIn("base currency", caught.exception.errors[0].message)

        complete = valid_payload()
        complete["base_currency_code"] = "EUR"
        complete["base_amount"] = "9.50"
        complete["fx_rate"] = "0.92682927"
        event = parse_transaction_event(complete)
        self.assertEqual(event.base_amount, Decimal("9.50"))
        self.assertEqual(event.fx_rate, Decimal("0.92682927"))

    def test_missing_required_fields_are_reported_together(self) -> None:
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event({"schema_version": "1", "event_type": EVENT_TYPE})
        fields = {error.field for error in caught.exception.errors}
        self.assertIn("source_system", fields)
        self.assertIn("source_event_id", fields)
        self.assertIn("account_reference", fields)
        self.assertIn("amount", fields)
        self.assertIn("event_timestamp", fields)

    def test_development_demonstration_event_is_marked_and_valid(self) -> None:
        event = parse_transaction_event(DEMONSTRATION_EVENT)
        self.assertEqual(event.source_system, SOURCE_SYSTEM)
        self.assertEqual(event.source_system, "amlog-development")
        self.assertTrue(event.account_reference.startswith("AMLOG-DEV-"))
        self.assertTrue(event.source_event_id.startswith("AMLOG-DEV-"))
        self.assertEqual(event.amount, Decimal("125.50"))
        self.assertEqual(event.event_timestamp, datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc))

    def test_identical_payment_details_do_not_conflict(self) -> None:
        event = parse_transaction_event(valid_payload())
        account_id = uuid.uuid4()
        customer_id = uuid.uuid4()
        stored = {
            "account_id": account_id,
            "customer_id": customer_id,
            "counterparty_reference": event.counterparty_reference,
            "counterparty_name": None,
            "counterparty_country_code": None,
            "counterparty_institution_reference": None,
            "payment_channel": event.payment_channel,
            "transaction_type": event.transaction_type,
            "direction": event.direction,
            "amount": Decimal("10.25000000"),
            "currency_code": "USD",
            "base_currency_code": None,
            "base_amount": None,
            "fx_rate": None,
            "event_timestamp": datetime(2026, 3, 1, 12, 34, 5, tzinfo=timezone(timedelta(hours=-2, minutes=-30))),
            "booked_at": None,
            "correlation_id": event.correlation_id,
            "corrects_transaction_id": None,
        }
        self.assertEqual(differing_fields(event, account_id, customer_id, stored), ())

    def test_changed_amount_is_a_material_conflict(self) -> None:
        event = parse_transaction_event(valid_payload())
        account_id = uuid.uuid4()
        customer_id = uuid.uuid4()
        stored = {
            "account_id": account_id,
            "customer_id": customer_id,
            "counterparty_reference": event.counterparty_reference,
            "counterparty_name": None,
            "counterparty_country_code": None,
            "counterparty_institution_reference": None,
            "payment_channel": event.payment_channel,
            "transaction_type": event.transaction_type,
            "direction": event.direction,
            "amount": Decimal("10.26"),
            "currency_code": "USD",
            "base_currency_code": None,
            "base_amount": None,
            "fx_rate": None,
            "event_timestamp": event.event_timestamp,
            "booked_at": None,
            "correlation_id": event.correlation_id,
            "corrects_transaction_id": None,
        }
        self.assertEqual(differing_fields(event, account_id, customer_id, stored), ("amount",))

    def test_schema_2_accepts_a_source_account_id_without_a_canonical_reference(self) -> None:
        payload = valid_payload()
        payload["schema_version"] = 2
        del payload["account_reference"]
        payload["source_account_id"] = "WALLET-100"
        event = parse_transaction_event(payload)
        self.assertEqual(event.schema_version, "2")
        self.assertIsNone(event.account_reference)
        self.assertEqual(event.source_account_id, "WALLET-100")

    def test_schema_1_rejects_a_source_account_id_without_a_database_call(self) -> None:
        payload = valid_payload()
        payload["source_account_id"] = "WALLET-100"
        result = TransactionIngestor(ExplodingConnection()).ingest(payload)
        self.assertEqual(result.reason, "invalid_event")
        self.assertEqual(result.errors[0].field, "source_account_id")
        self.assertIn("schema 2", result.errors[0].message)

    def test_schema_2_requires_at_least_one_account_identifier(self) -> None:
        payload = valid_payload()
        payload["schema_version"] = "2"
        del payload["account_reference"]
        with self.assertRaises(EventValidationError) as caught:
            parse_transaction_event(payload)
        self.assertEqual(caught.exception.errors[0].field, "account_reference")
        self.assertIn("source_account_id", caught.exception.errors[0].message)

    def test_development_event_remains_schema_1(self) -> None:
        event = parse_transaction_event(DEMONSTRATION_EVENT)
        self.assertEqual(event.schema_version, "1")
        self.assertEqual(event.account_reference, "AMLOG-DEV-ACCOUNT-001")
        self.assertIsNone(event.source_account_id)


if __name__ == "__main__":
    unittest.main()

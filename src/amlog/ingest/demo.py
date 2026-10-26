"""Insert one development payment the user can inspect.

The customer, account, and payment are marked with the source system
``amlog-development`` and the prefix ``AMLOG-DEV-``. They are not
operational history and they are not training data. Running this command
again stores nothing extra when the same payment is already present.

The command reads AMLOG_DATABASE_URL from the process environment. It does
not read a .env file and it does not contact AWS.
"""

from __future__ import annotations

import sys
from datetime import timezone
from typing import Any
from uuid import UUID

from amlog.ingest.service import TransactionIngestor, load_recorded_transaction

SOURCE_SYSTEM = "amlog-development"
CUSTOMER_REFERENCE = "AMLOG-DEV-CUSTOMER-001"
LEGAL_NAME = "AMLOG development fixture"
CUSTOMER_TYPE = "organization"
ACCOUNT_REFERENCE = "AMLOG-DEV-ACCOUNT-001"
SOURCE_ACCOUNT_ID = "AMLOG-DEV-ACCOUNT-001"
ACCOUNT_TYPE = "deposit"
ACCOUNT_CURRENCY = "USD"
SOURCE_EVENT_ID = "AMLOG-DEV-PAYMENT-001"
CORRELATION_ID = UUID("6f1c1a4e-8a0d-4c3e-9b2a-0d5e7c8b9a11")

DEMONSTRATION_EVENT = {
    "schema_version": "1",
    "event_type": "payment_event_received",
    "source_system": SOURCE_SYSTEM,
    "source_event_id": SOURCE_EVENT_ID,
    "account_reference": ACCOUNT_REFERENCE,
    "transaction_type": "transfer",
    "payment_channel": "wire",
    "direction": "debit",
    "amount": "125.50",
    "currency_code": "USD",
    "event_timestamp": "2026-01-15T14:30:00+00:00",
    "booked_at": "2026-01-15T14:35:00+00:00",
    "counterparty_reference": "AMLOG-DEV-COUNTERPARTY-001",
    "counterparty_name": "AMLOG development counterparty",
    "counterparty_country_code": "CA",
    "correlation_id": str(CORRELATION_ID),
}


def main() -> int:
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. No database was contacted.", file=sys.stderr)
        return 2

    from amlog.config import load_settings

    try:
        settings = load_settings()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if settings.database_url is None:
        print(
            "AMLOG_DATABASE_URL is not set. No database was contacted. "
            "Load the local environment and run this command again.",
            file=sys.stderr,
        )
        return 2

    connection = psycopg.connect(settings.database_url, connect_timeout=5)
    connection.autocommit = False
    try:
        customer_id = _ensure_customer(connection)
        _ensure_account(connection, customer_id)
        result = TransactionIngestor(connection).ingest(DEMONSTRATION_EVENT)
        if not result.stored:
            connection.rollback()
            print(result.message, file=sys.stderr)
            return 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    reading = psycopg.connect(settings.database_url, connect_timeout=5)
    try:
        stored = load_recorded_transaction(reading, SOURCE_SYSTEM, SOURCE_EVENT_ID)
    finally:
        reading.close()
    if stored is None:
        print("The demonstration payment was not found after commit.", file=sys.stderr)
        return 1
    _print_record(result.outcome, stored)
    return 0


def _ensure_customer(connection: Any) -> UUID:
    row = connection.execute(
        """
        SELECT customer_id, customer_type, legal_name
        FROM amlog.customers
        WHERE customer_reference = %s
        """,
        (CUSTOMER_REFERENCE,),
    ).fetchone()
    if row is None:
        created = connection.execute(
            """
            INSERT INTO amlog.customers (
                customer_reference, customer_type, legal_name, lifecycle_status
            )
            VALUES (%s, %s, %s, 'active')
            RETURNING customer_id
            """,
            (CUSTOMER_REFERENCE, CUSTOMER_TYPE, LEGAL_NAME),
        ).fetchone()
        return created[0]
    customer_id, customer_type, legal_name = row
    if customer_type != CUSTOMER_TYPE or legal_name != LEGAL_NAME:
        raise RuntimeError(
            f"{CUSTOMER_REFERENCE} already exists and does not match the development fixture. "
            "No demonstration payment was stored."
        )
    return customer_id


def _ensure_account(connection: Any, customer_id: UUID) -> UUID:
    row = connection.execute(
        """
        SELECT account_id, customer_id, source_system, source_account_id, account_type, currency_code
        FROM amlog.accounts
        WHERE account_reference = %s
        """,
        (ACCOUNT_REFERENCE,),
    ).fetchone()
    if row is None:
        created = connection.execute(
            """
            INSERT INTO amlog.accounts (
                customer_id,
                account_reference,
                source_system,
                source_account_id,
                account_type,
                currency_code,
                lifecycle_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'open')
            RETURNING account_id
            """,
            (
                customer_id,
                ACCOUNT_REFERENCE,
                SOURCE_SYSTEM,
                SOURCE_ACCOUNT_ID,
                ACCOUNT_TYPE,
                ACCOUNT_CURRENCY,
            ),
        ).fetchone()
        return created[0]
    account_id, owner_id, source_system, source_account_id, account_type, currency_code = row
    currency_code = str(currency_code).rstrip()
    if (
        owner_id != customer_id
        or source_system != SOURCE_SYSTEM
        or source_account_id != SOURCE_ACCOUNT_ID
        or account_type != ACCOUNT_TYPE
        or currency_code != ACCOUNT_CURRENCY
    ):
        raise RuntimeError(
            f"{ACCOUNT_REFERENCE} already exists and does not match the development fixture. "
            "No demonstration payment was stored."
        )
    return account_id


def _print_record(outcome: str, stored: dict[str, Any]) -> None:
    amount = format(stored["amount"], "f")
    event_time = stored["event_timestamp"].astimezone(timezone.utc).isoformat()
    print(f"Outcome: {outcome}")
    print(f"Transaction: {stored['transaction_id']}")
    print(f"Account reference: {stored['account_reference']}")
    print(f"Customer reference: {stored['customer_reference']}")
    print(f"Customer name: {stored['legal_name']}")
    print(f"Amount: {amount} {stored['currency_code']} {stored['direction']}")
    print(f"Channel: {stored['payment_channel']} {stored['transaction_type']}")
    print(f"Event time: {event_time}")
    print(f"Source: {stored['source_system']} / {stored['source_event_id']}")
    print(f"Counterparty: {stored['counterparty_reference']}")
    print("This is a development fixture, not an operational payment.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Demonstration failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

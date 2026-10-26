"""Store one validated transaction event.

The ingestor commits when it owns the database transaction. If the caller
has already opened a transaction, the write joins that transaction and the
caller decides whether to commit. A failed write leaves no partial row:
the transaction insert and its audit row succeed or roll back together.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from amlog.ingest.contract import (
    EventValidationError,
    FieldError,
    TransactionEvent,
    parse_transaction_event,
)

_COMPARED_FIELDS = (
    "account_id",
    "customer_id",
    "counterparty_reference",
    "counterparty_name",
    "counterparty_country_code",
    "counterparty_institution_reference",
    "payment_channel",
    "transaction_type",
    "direction",
    "amount",
    "currency_code",
    "base_currency_code",
    "base_amount",
    "fx_rate",
    "event_timestamp",
    "booked_at",
    "correlation_id",
    "corrects_transaction_id",
)


@dataclass(frozen=True)
class IngestionResult:
    """The outcome of accepting or refusing one event.

    ``recorded`` means a new row was stored. ``already_recorded`` means this
    exact event was stored before. ``rejected`` means nothing new was stored.
    """

    outcome: str
    reason: str | None
    message: str
    transaction_id: UUID | None = None
    account_id: UUID | None = None
    customer_id: UUID | None = None
    source_system: str | None = None
    source_event_id: str | None = None
    audit_event_id: UUID | None = None
    errors: tuple[FieldError, ...] = ()
    conflicting_fields: tuple[str, ...] = ()

    @property
    def stored(self) -> bool:
        return self.outcome in {"recorded", "already_recorded"}


class IngestionPersistenceError(RuntimeError):
    """A validated event could not be committed. No partial row is left behind."""


class TransactionIngestor:
    """Ingest one event through a caller-supplied PostgreSQL connection.

    This is the interface a future event consumer should call. It does not
    subscribe to a queue, and it does not contact AWS.
    """

    def __init__(self, connection: Any):
        self._connection = connection

    def ingest(self, payload: Mapping[str, Any] | TransactionEvent) -> IngestionResult:
        try:
            event = payload if isinstance(payload, TransactionEvent) else parse_transaction_event(payload)
        except EventValidationError as exc:
            source_system = _optional_text(payload, "source_system") if isinstance(payload, Mapping) else None
            source_event_id = _optional_text(payload, "source_event_id") if isinstance(payload, Mapping) else None
            return IngestionResult(
                outcome="rejected",
                reason="invalid_event",
                message=str(exc),
                source_system=source_system,
                source_event_id=source_event_id,
                errors=exc.errors,
            )

        try:
            with self._connection.transaction():
                return self._store(event)
        except IngestionPersistenceError:
            raise
        except Exception as exc:
            if exc.__class__.__module__.startswith("psycopg"):
                raise IngestionPersistenceError(
                    "The database did not store the transaction. "
                    f"PostgreSQL reported: {exc.__class__.__name__}: {exc}"
                ) from exc
            raise

    def _store(self, event: TransactionEvent) -> IngestionResult:
        account_id, customer_id, refusal = self._resolve_account(event)
        if refusal is not None:
            return refusal

        inserted = self._connection.execute(
            """
            INSERT INTO amlog.transactions (
                source_system,
                source_event_id,
                account_id,
                customer_id,
                counterparty_reference,
                counterparty_name,
                counterparty_country_code,
                counterparty_institution_reference,
                payment_channel,
                transaction_type,
                direction,
                amount,
                currency_code,
                base_currency_code,
                base_amount,
                fx_rate,
                event_timestamp,
                booked_at,
                correlation_id
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT ON CONSTRAINT transactions_source_event_unique DO NOTHING
            RETURNING transaction_id
            """,
            (
                event.source_system,
                event.source_event_id,
                account_id,
                customer_id,
                event.counterparty_reference,
                event.counterparty_name,
                event.counterparty_country_code,
                event.counterparty_institution_reference,
                event.payment_channel,
                event.transaction_type,
                event.direction,
                event.amount,
                event.currency_code,
                event.base_currency_code,
                event.base_amount,
                event.fx_rate,
                event.event_timestamp,
                event.booked_at,
                event.correlation_id,
            ),
        ).fetchone()
        if inserted is not None:
            transaction_id = inserted[0]
            audit_event_id = self._audit(event, transaction_id, account_id, customer_id)
            return IngestionResult(
                outcome="recorded",
                reason=None,
                message="Transaction stored.",
                transaction_id=transaction_id,
                account_id=account_id,
                customer_id=customer_id,
                source_system=event.source_system,
                source_event_id=event.source_event_id,
                audit_event_id=audit_event_id,
            )

        stored = _load_identity(self._connection, event.source_system, event.source_event_id)
        conflicts = differing_fields(event, account_id, customer_id, stored)
        if not conflicts:
            return IngestionResult(
                outcome="already_recorded",
                reason="identical_replay",
                message="This event was already stored. No additional transaction was created.",
                transaction_id=stored["transaction_id"],
                account_id=stored["account_id"],
                customer_id=stored["customer_id"],
                source_system=event.source_system,
                source_event_id=event.source_event_id,
            )

        listed = ", ".join(conflicts)
        return IngestionResult(
            outcome="rejected",
            reason="idempotency_conflict",
            message=(
                "This source event was already stored with different payment details "
                f"({listed}). The stored transaction was left unchanged."
            ),
            transaction_id=stored["transaction_id"],
            account_id=stored["account_id"],
            customer_id=stored["customer_id"],
            source_system=event.source_system,
            source_event_id=event.source_event_id,
            errors=(
                FieldError(
                    "source_event_id",
                    "This source event was already stored with different payment details.",
                ),
            ),
            conflicting_fields=conflicts,
            )

    def _resolve_account(
        self,
        event: TransactionEvent,
    ) -> tuple[UUID | None, UUID | None, IngestionResult | None]:
        """Find the account row and the customer stored on that row.

        A source account id is matched with the event's source system.
        When both identifiers are present, they must select the same account.
        This method does not insert a customer or an account.
        """

        by_reference = None
        if event.account_reference is not None:
            by_reference = self._connection.execute(
                """
                SELECT account_id, customer_id
                FROM amlog.accounts
                WHERE account_reference = %s
                """,
                (event.account_reference,),
            ).fetchone()
        by_source = None
        if event.source_account_id is not None:
            by_source = self._connection.execute(
                """
                SELECT account_id, customer_id
                FROM amlog.accounts
                WHERE source_system = %s
                  AND source_account_id = %s
                """,
                (event.source_system, event.source_account_id),
            ).fetchone()

        if event.account_reference is not None and event.source_account_id is not None:
            same_account = (
                by_reference is not None
                and by_source is not None
                and by_reference[0] == by_source[0]
            )
            if not same_account:
                return None, None, self._inconsistent_account(event, by_reference, by_source)
            return by_reference[0], by_reference[1], None

        if event.account_reference is not None:
            if by_reference is None:
                return None, None, _account_not_found(
                    event,
                    "account_reference",
                    f"No account is registered with reference {event.account_reference!r}. "
                    "The event was not stored.",
                    "No account is registered with this reference.",
                )
            return by_reference[0], by_reference[1], None

        if by_source is None:
            return None, None, _account_not_found(
                event,
                "source_account_id",
                "No account is registered for this source system and source account identifier. "
                "The event was not stored, and no customer or account was created.",
                "No account is registered for this source system and source account identifier.",
            )
        return by_source[0], by_source[1], None

    def _inconsistent_account(
        self,
        event: TransactionEvent,
        by_reference: tuple[Any, Any] | None,
        by_source: tuple[Any, Any] | None,
    ) -> IngestionResult:
        if by_reference is None and by_source is None:
            message = (
                "Neither account_reference nor source_account_id is registered. "
                "The event was not stored, and no customer or account was created."
            )
        elif by_reference is not None and by_source is not None:
            message = (
                "account_reference and source_account_id are registered to different accounts. "
                "Neither identifier was selected, and the event was not stored."
            )
        elif by_reference is None:
            message = (
                "account_reference is not registered. "
                "The source account was not used in its place, and the event was not stored."
            )
        else:
            message = (
                "source_account_id is not registered for this source system. "
                "The account reference was not used in its place, and the event was not stored."
            )
        return IngestionResult(
            outcome="rejected",
            reason="inconsistent_account_reference",
            message=message,
            source_system=event.source_system,
            source_event_id=event.source_event_id,
            errors=(
                FieldError(
                    "account_reference",
                    "This account reference does not identify the same account as source_account_id.",
                ),
                FieldError(
                    "source_account_id",
                    "This source account identifier does not identify the same account as account_reference.",
                ),
            ),
        )

    def _audit(
        self,
        event: TransactionEvent,
        transaction_id: UUID,
        account_id: UUID,
        customer_id: UUID,
    ) -> UUID:
        from psycopg.types.json import Json

        row = self._connection.execute(
            """
            SELECT amlog.record_audit_event(
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::uuid,
                %s::uuid,
                %s::text,
                %s::jsonb,
                %s::jsonb,
                %s::jsonb
            )
            """,
            (
                "system",
                "amlog.transaction_ingest",
                "transaction_recorded",
                "amlog",
                "transactions",
                transaction_id,
                event.correlation_id,
                f"{event.source_system}:{event.source_event_id}",
                None,
                Json(_resulting_document(event, transaction_id, account_id, customer_id)),
                Json(
                    {
                        "schema_version": event.schema_version,
                        "event_type": event.event_type,
                    }
                ),
            ),
        ).fetchone()
        return row[0]


def load_recorded_transaction(connection: Any, source_system: str, source_event_id: str) -> dict[str, Any] | None:
    """Return the stored payment, its account, and its customer, if present."""

    row = connection.execute(
        """
        SELECT
            t.transaction_id,
            t.source_system,
            t.source_event_id,
            t.account_id,
            t.customer_id,
            t.direction,
            t.amount,
            btrim(t.currency_code::text) AS currency_code,
            t.payment_channel,
            t.transaction_type,
            t.event_timestamp,
            t.booked_at,
            t.ingested_at,
            t.correlation_id,
            t.counterparty_reference,
            t.counterparty_name,
            btrim(t.counterparty_country_code::text) AS counterparty_country_code,
            a.account_reference,
            c.customer_reference,
            c.legal_name
        FROM amlog.transactions AS t
        JOIN amlog.accounts AS a ON a.account_id = t.account_id
        JOIN amlog.customers AS c ON c.customer_id = t.customer_id
        WHERE t.source_system = %s
          AND t.source_event_id = %s
        """,
        (source_system, source_event_id),
    ).fetchone()
    if row is None:
        return None
    columns = (
        "transaction_id",
        "source_system",
        "source_event_id",
        "account_id",
        "customer_id",
        "direction",
        "amount",
        "currency_code",
        "payment_channel",
        "transaction_type",
        "event_timestamp",
        "booked_at",
        "ingested_at",
        "correlation_id",
        "counterparty_reference",
        "counterparty_name",
        "counterparty_country_code",
        "account_reference",
        "customer_reference",
        "legal_name",
    )
    return dict(zip(columns, row, strict=True))


def differing_fields(
    event: TransactionEvent,
    account_id: UUID,
    customer_id: UUID,
    stored: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return the payment fields whose stored values differ from this event.

    Identity columns and ingest clocks are not payment details. The same
    instant expressed with two offsets is not a difference.
    """

    expected = {
        "account_id": account_id,
        "customer_id": customer_id,
        "counterparty_reference": event.counterparty_reference,
        "counterparty_name": event.counterparty_name,
        "counterparty_country_code": event.counterparty_country_code,
        "counterparty_institution_reference": event.counterparty_institution_reference,
        "payment_channel": event.payment_channel,
        "transaction_type": event.transaction_type,
        "direction": event.direction,
        "amount": event.amount,
        "currency_code": event.currency_code,
        "base_currency_code": event.base_currency_code,
        "base_amount": event.base_amount,
        "fx_rate": event.fx_rate,
        "event_timestamp": event.event_timestamp,
        "booked_at": event.booked_at,
        "correlation_id": event.correlation_id,
        "corrects_transaction_id": None,
    }
    different: list[str] = []
    for field in _COMPARED_FIELDS:
        if not _same_value(field, expected[field], stored.get(field)):
            different.append(field)
    return tuple(different)


def _load_identity(connection: Any, source_system: str, source_event_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            transaction_id,
            account_id,
            customer_id,
            counterparty_reference,
            counterparty_name,
            btrim(counterparty_country_code::text) AS counterparty_country_code,
            counterparty_institution_reference,
            payment_channel,
            transaction_type,
            direction,
            amount,
            btrim(currency_code::text) AS currency_code,
            btrim(base_currency_code::text) AS base_currency_code,
            base_amount,
            fx_rate,
            event_timestamp,
            booked_at,
            correlation_id,
            corrects_transaction_id
        FROM amlog.transactions
        WHERE source_system = %s
          AND source_event_id = %s
        """,
        (source_system, source_event_id),
    ).fetchone()
    if row is None:
        raise IngestionPersistenceError(
            "The source event conflicted with an existing row, but that row could not be read back."
        )
    columns = (
        "transaction_id",
        "account_id",
        "customer_id",
        "counterparty_reference",
        "counterparty_name",
        "counterparty_country_code",
        "counterparty_institution_reference",
        "payment_channel",
        "transaction_type",
        "direction",
        "amount",
        "currency_code",
        "base_currency_code",
        "base_amount",
        "fx_rate",
        "event_timestamp",
        "booked_at",
        "correlation_id",
        "corrects_transaction_id",
    )
    return dict(zip(columns, row, strict=True))


def _same_value(field: str, expected: Any, stored: Any) -> bool:
    if field in {"event_timestamp", "booked_at"}:
        return _same_instant(expected, stored)
    if field in {"amount", "base_amount", "fx_rate"}:
        return _same_decimal(expected, stored)
    if field in {"currency_code", "base_currency_code", "counterparty_country_code"} and isinstance(stored, str):
        stored = stored.rstrip() or None
    if expected is None or stored is None:
        return expected is None and stored is None
    return expected == stored


def _same_instant(expected: datetime | None, stored: datetime | None) -> bool:
    if expected is None or stored is None:
        return expected is None and stored is None
    if expected.tzinfo is None or stored.tzinfo is None:
        return False
    return expected.astimezone(timezone.utc) == stored.astimezone(timezone.utc)


def _same_decimal(expected: Decimal | None, stored: Decimal | None) -> bool:
    if expected is None or stored is None:
        return expected is None and stored is None
    return Decimal(expected) == Decimal(stored)


def _account_not_found(
    event: TransactionEvent,
    field: str,
    message: str,
    field_message: str,
) -> IngestionResult:
    return IngestionResult(
        outcome="rejected",
        reason="account_not_found",
        message=message,
        source_system=event.source_system,
        source_event_id=event.source_event_id,
        errors=(FieldError(field, field_message),),
    )


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip() and value == value.strip():
        return value
    return None


def _resulting_document(
    event: TransactionEvent,
    transaction_id: UUID,
    account_id: UUID,
    customer_id: UUID,
) -> dict[str, Any]:
    return {
        "transaction_id": str(transaction_id),
        "account_id": str(account_id),
        "customer_id": str(customer_id),
        "source_system": event.source_system,
        "source_event_id": event.source_event_id,
        "account_reference": event.account_reference,
        "source_account_id": event.source_account_id,
        "payment_channel": event.payment_channel,
        "transaction_type": event.transaction_type,
        "direction": event.direction,
        "amount": format(event.amount, "f"),
        "currency_code": event.currency_code,
        "base_currency_code": event.base_currency_code,
        "base_amount": None if event.base_amount is None else format(event.base_amount, "f"),
        "fx_rate": None if event.fx_rate is None else format(event.fx_rate, "f"),
        "event_timestamp": event.event_timestamp.isoformat(),
        "booked_at": None if event.booked_at is None else event.booked_at.isoformat(),
        "counterparty_reference": event.counterparty_reference,
        "correlation_id": None if event.correlation_id is None else str(event.correlation_id),
    }

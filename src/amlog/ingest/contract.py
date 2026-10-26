"""Contract for a financial transaction event.

Schema 1 identifies the account with the AMLOG account reference.
Schema 2 can identify it with the source system's own account identifier.
An event does not name the AMLOG customer, and it cannot carry a training
label or a fraud judgment. Those decisions belong to later, separate steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

SCHEMA_VERSION = "1"
SCHEMA_VERSION_2 = "2"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION, SCHEMA_VERSION_2)
EVENT_TYPE = "payment_event_received"

_MONEY_QUANTUM = Decimal("0.00000001")
_MONEY_MAX = Decimal("999999999999999999999999999999.99999999")
_FX_QUANTUM = Decimal("0.000000000000000001")
_FX_MAX = Decimal("99999999999999999999.999999999999999999")

_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "source_system",
        "source_event_id",
        "account_reference",
        "transaction_type",
        "payment_channel",
        "direction",
        "amount",
        "currency_code",
        "event_timestamp",
        "counterparty_reference",
        "counterparty_name",
        "counterparty_country_code",
        "counterparty_institution_reference",
        "correlation_id",
        "booked_at",
        "base_currency_code",
        "base_amount",
        "fx_rate",
    }
)

_FORBIDDEN_FIELDS = {
    "customer_id": "Customer ownership is resolved from the account and cannot be supplied on the event.",
    "account_id": (
        "Use account_reference or, on schema 2, source_account_id. "
        "An internal account identifier is not accepted."
    ),
    "label": "Training labels are not part of an operational transaction event.",
    "label_value": "Training labels are not part of an operational transaction event.",
    "label_source": "Training labels are not part of an operational transaction event.",
    "data_origin": "Label provenance is not part of an operational transaction event.",
    "fraud": "Fraud judgments are not part of an operational transaction event.",
    "is_fraud": "Fraud judgments are not part of an operational transaction event.",
    "suspicious": "Fraud judgments are not part of an operational transaction event.",
    "risk_score": "Risk scores are not part of an operational transaction event.",
    "score": "Risk scores are not part of an operational transaction event.",
    "disposition": "Investigation outcomes are not part of an operational transaction event.",
}

_MISSING = object()


@dataclass(frozen=True)
class FieldError:
    """One problem a caller can correct in the submitted event."""

    field: str
    message: str


class EventValidationError(ValueError):
    """The event does not satisfy the transaction contract."""

    def __init__(self, errors: tuple[FieldError, ...]):
        if not errors:
            raise ValueError("EventValidationError requires at least one field error.")
        self.errors = errors
        detail = "; ".join(f"{error.field}: {error.message}" for error in errors)
        super().__init__(detail)


@dataclass(frozen=True)
class TransactionEvent:
    """A validated event, ready to resolve and store.

    ``account_reference`` is the AMLOG account reference. ``source_account_id``
    is the identifier used by ``source_system``. Schema 2 may carry either
    or both. The customer is not on the event. Amounts are ``Decimal``
    values, never binary floats.
    """

    schema_version: str
    event_type: str
    source_system: str
    source_event_id: str
    account_reference: str | None
    transaction_type: str
    payment_channel: str
    direction: str
    amount: Decimal
    currency_code: str
    event_timestamp: datetime
    counterparty_reference: str | None = None
    counterparty_name: str | None = None
    counterparty_country_code: str | None = None
    counterparty_institution_reference: str | None = None
    correlation_id: UUID | None = None
    booked_at: datetime | None = None
    base_currency_code: str | None = None
    base_amount: Decimal | None = None
    fx_rate: Decimal | None = None
    source_account_id: str | None = None


def parse_transaction_event(payload: object) -> TransactionEvent:
    """Validate a mapping and return the event.

    A JSON number is rejected for money fields. Callers that start from
    JSON must keep amounts as strings so the value stays exact.
    """

    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        raise EventValidationError(
            (
                FieldError(
                    "event",
                    "A transaction event must be an object of named fields.",
                ),
            )
        )

    errors: list[FieldError] = []
    schema_version = _schema_version(payload.get("schema_version", _MISSING), errors)
    allowed_fields = _ALLOWED_FIELDS | {"source_account_id"} if schema_version == SCHEMA_VERSION_2 else _ALLOWED_FIELDS
    for key in payload:
        if key in _FORBIDDEN_FIELDS:
            errors.append(FieldError(str(key), _FORBIDDEN_FIELDS[key]))
        elif key == "source_account_id" and schema_version != SCHEMA_VERSION_2:
            errors.append(
                FieldError(
                    "source_account_id",
                    "source_account_id is accepted on schema 2. "
                    "Schema 1 identifies the account with account_reference.",
                )
            )
        elif key not in allowed_fields:
            label = schema_version if schema_version is not None else "an unsupported schema"
            errors.append(
                FieldError(
                    str(key),
                    f"This field is not part of transaction event schema {label}.",
                )
            )

    event_type = _exact_choice(
        payload.get("event_type", _MISSING),
        "event_type",
        {EVENT_TYPE},
        f"Event type must be {EVENT_TYPE!r}.",
        errors,
    )
    source_system = _required_token(payload.get("source_system", _MISSING), "source_system", errors)
    source_event_id = _required_token(
        payload.get("source_event_id", _MISSING),
        "source_event_id",
        errors,
    )
    account_reference, source_account_id = _account_identifiers(
        payload,
        schema_version,
        errors,
    )
    transaction_type = _required_token(
        payload.get("transaction_type", _MISSING),
        "transaction_type",
        errors,
    )
    payment_channel = _required_token(
        payload.get("payment_channel", _MISSING),
        "payment_channel",
        errors,
    )
    direction = _exact_choice(
        payload.get("direction", _MISSING),
        "direction",
        {"debit", "credit"},
        "Direction must be 'debit' or 'credit'.",
        errors,
    )
    amount = _money(payload.get("amount", _MISSING), "amount", errors, required=True)
    currency_code = _currency(payload.get("currency_code", _MISSING), "currency_code", errors, required=True)
    event_timestamp = _timestamp(
        payload.get("event_timestamp", _MISSING),
        "event_timestamp",
        errors,
        required=True,
    )
    counterparty_reference = _optional_token(
        payload.get("counterparty_reference", _MISSING),
        "counterparty_reference",
        errors,
    )
    counterparty_name = _optional_token(
        payload.get("counterparty_name", _MISSING),
        "counterparty_name",
        errors,
    )
    counterparty_country_code = _country(
        payload.get("counterparty_country_code", _MISSING),
        errors,
    )
    counterparty_institution_reference = _optional_token(
        payload.get("counterparty_institution_reference", _MISSING),
        "counterparty_institution_reference",
        errors,
    )
    correlation_id = _uuid(payload.get("correlation_id", _MISSING), "correlation_id", errors)
    booked_at = _timestamp(payload.get("booked_at", _MISSING), "booked_at", errors, required=False)
    base_currency_code = _currency(
        payload.get("base_currency_code", _MISSING),
        "base_currency_code",
        errors,
        required=False,
    )
    base_amount = _money(payload.get("base_amount", _MISSING), "base_amount", errors, required=False)
    fx_rate = _fx_rate(payload.get("fx_rate", _MISSING), errors)
    _check_base_pair(base_currency_code, base_amount, fx_rate, errors)

    if (
        errors
        or schema_version is None
        or event_type is None
        or source_system is None
        or source_event_id is None
        or (account_reference is None and source_account_id is None)
        or transaction_type is None
        or payment_channel is None
        or direction is None
        or not isinstance(amount, Decimal)
        or not isinstance(currency_code, str)
        or not isinstance(event_timestamp, datetime)
    ):
        if not errors:
            raise RuntimeError("The event could not be validated and no field error was recorded.")
        raise EventValidationError(tuple(errors))

    return TransactionEvent(
        schema_version=schema_version,
        event_type=event_type,
        source_system=source_system,
        source_event_id=source_event_id,
        account_reference=account_reference,
        transaction_type=transaction_type,
        payment_channel=payment_channel,
        direction=direction,
        amount=amount,
        currency_code=currency_code,
        event_timestamp=event_timestamp,
        counterparty_reference=counterparty_reference,
        counterparty_name=counterparty_name,
        counterparty_country_code=counterparty_country_code,
        counterparty_institution_reference=counterparty_institution_reference,
        correlation_id=correlation_id,
        booked_at=booked_at,
        base_currency_code=base_currency_code,
        base_amount=base_amount,
        fx_rate=fx_rate,
        source_account_id=source_account_id,
    )


def _schema_version(value: Any, errors: list[FieldError]) -> str | None:
    if value is _MISSING or value is None:
        errors.append(FieldError("schema_version", "Schema version is required."))
        return None
    if isinstance(value, bool):
        errors.append(
            FieldError(
                "schema_version",
                "Schema version must be 1 or 2. A boolean is not a version.",
            )
        )
        return None
    if value == SCHEMA_VERSION or (type(value) is int and value == 1):
        return SCHEMA_VERSION
    if value == SCHEMA_VERSION_2 or (type(value) is int and value == 2):
        return SCHEMA_VERSION_2
    supported = " and ".join(SUPPORTED_SCHEMA_VERSIONS)
    errors.append(
        FieldError(
            "schema_version",
            f"Schema version {value!r} is not supported. Supported versions: {supported}.",
        )
    )
    return None


def _account_identifiers(
    payload: Mapping[str, Any],
    schema_version: str | None,
    errors: list[FieldError],
) -> tuple[str | None, str | None]:
    """Return the canonical reference and the source account id.

    Schema 1 still requires the canonical reference. Schema 2 accepts the
    source account id, the canonical reference, or both. Agreement between
    the two is checked after the account rows are read, not here.
    """

    reference_raw = payload.get("account_reference", _MISSING)
    source_raw = payload.get("source_account_id", _MISSING)
    if schema_version == SCHEMA_VERSION_2:
        account_reference = _optional_token(reference_raw, "account_reference", errors)
        source_account_id = _optional_token(source_raw, "source_account_id", errors)
        reference_absent = reference_raw is _MISSING or reference_raw is None
        source_absent = source_raw is _MISSING or source_raw is None
        if reference_absent and source_absent:
            errors.append(
                FieldError(
                    "account_reference",
                    "Schema 2 requires account_reference, source_account_id, or both.",
                )
            )
        return account_reference, source_account_id

    return _required_token(reference_raw, "account_reference", errors), None


def _exact_choice(
    value: Any,
    field: str,
    allowed: set[str],
    message: str,
    errors: list[FieldError],
) -> str | None:
    if value is _MISSING or value is None:
        errors.append(FieldError(field, message))
        return None
    if isinstance(value, str) and value in allowed:
        return value
    errors.append(FieldError(field, message))
    return None


def _required_token(value: Any, field: str, errors: list[FieldError]) -> str | None:
    if value is _MISSING or value is None:
        errors.append(FieldError(field, "This field is required."))
        return None
    if not isinstance(value, str):
        errors.append(FieldError(field, "This field must be text."))
        return None
    if value == "" or value != value.strip():
        errors.append(
            FieldError(
                field,
                "This field must be non-blank text without leading or trailing spaces.",
            )
        )
        return None
    return value


def _optional_token(value: Any, field: str, errors: list[FieldError]) -> str | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        errors.append(FieldError(field, "This field must be text when it is supplied."))
        return None
    if value == "" or value != value.strip():
        errors.append(
            FieldError(
                field,
                "This field must be omitted or be non-blank text without leading or trailing spaces.",
            )
        )
        return None
    return value


def _money(value: Any, field: str, errors: list[FieldError], *, required: bool) -> Decimal | None:
    if value is _MISSING or value is None:
        if required:
            errors.append(FieldError(field, "Amount is required and must be a decimal string."))
        return None
    parsed = _exact_decimal(value, field, errors)
    if parsed is None:
        return None
    if not parsed.is_finite():
        errors.append(FieldError(field, "Amount must be a finite decimal number."))
        return None
    if parsed < 0:
        errors.append(FieldError(field, "Amount must be zero or positive. Direction carries the sign."))
        return None
    if parsed > _MONEY_MAX or parsed != parsed.quantize(_MONEY_QUANTUM):
        errors.append(
            FieldError(
                field,
                "Amount must fit an exact numeric value with at most 30 digits before the decimal point and 8 after it.",
            )
        )
        return None
    return parsed


def _fx_rate(value: Any, errors: list[FieldError]) -> Decimal | None:
    if value is _MISSING or value is None:
        return None
    parsed = _exact_decimal(value, "fx_rate", errors)
    if parsed is None:
        return None
    if not parsed.is_finite() or parsed <= 0:
        errors.append(FieldError("fx_rate", "Exchange rate must be a positive finite decimal number."))
        return None
    if parsed > _FX_MAX or parsed != parsed.quantize(_FX_QUANTUM):
        errors.append(
            FieldError(
                "fx_rate",
                "Exchange rate must fit an exact numeric value with at most 20 digits before the decimal point and 18 after it.",
            )
        )
        return None
    return parsed


def _exact_decimal(value: Any, field: str, errors: list[FieldError]) -> Decimal | None:
    if isinstance(value, bool) or isinstance(value, float):
        errors.append(
            FieldError(
                field,
                "This value must be a decimal string or an integer, not a floating-point number.",
            )
        )
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        if value == "" or value != value.strip():
            errors.append(
                FieldError(
                    field,
                    "This value must be a decimal string without leading or trailing spaces.",
                )
            )
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            errors.append(FieldError(field, "This value is not a decimal number."))
            return None
    errors.append(FieldError(field, "This value must be a decimal string or an integer."))
    return None


def _currency(value: Any, field: str, errors: list[FieldError], *, required: bool) -> str | None:
    if value is _MISSING or value is None:
        if required:
            errors.append(FieldError(field, "Currency must be three uppercase letters, such as USD."))
        return None
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha() and value.isupper():
        return value
    errors.append(FieldError(field, "Currency must be three uppercase letters, such as USD."))
    return None


def _country(value: Any, errors: list[FieldError]) -> str | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str) and len(value) == 2 and value.isascii() and value.isalpha() and value.isupper():
        return value
    errors.append(
        FieldError(
            "counterparty_country_code",
            "Country code must be two uppercase letters when it is supplied.",
        )
    )
    return None


def _timestamp(value: Any, field: str, errors: list[FieldError], *, required: bool) -> datetime | None:
    if value is _MISSING or value is None:
        if required:
            errors.append(
                FieldError(
                    field,
                    "Timestamp must include a time and a time zone offset, for example 2026-01-15T14:30:00+00:00.",
                )
            )
        return None
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
    if parsed is None:
        errors.append(
            FieldError(
                field,
                "Timestamp must include a time and a time zone offset, for example 2026-01-15T14:30:00+00:00.",
            )
        )
        return None
    offset = parsed.tzinfo.utcoffset(parsed) if parsed.tzinfo is not None else None
    if offset is None:
        errors.append(
            FieldError(
                field,
                "Timestamp must include a time zone offset, for example 2026-01-15T14:30:00+00:00.",
            )
        )
        return None
    return parsed.astimezone(timezone.utc)


def _uuid(value: Any, field: str, errors: list[FieldError]) -> UUID | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    errors.append(FieldError(field, "Correlation identifier must be a UUID when it is supplied."))
    return None


def _check_base_pair(
    base_currency: str | None,
    base_amount: Decimal | None,
    fx_rate: Decimal | None,
    errors: list[FieldError],
) -> None:
    if (base_currency is None) != (base_amount is None):
        errors.append(
            FieldError(
                "base_amount",
                "Base currency and base amount must both be supplied, or both be omitted.",
            )
        )
    if fx_rate is not None and (base_currency is None or base_amount is None):
        errors.append(
            FieldError(
                "fx_rate",
                "An exchange rate is accepted only together with a base currency and a base amount.",
            )
        )

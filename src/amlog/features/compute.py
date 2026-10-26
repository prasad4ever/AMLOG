"""Calculate alert-behavior features from payments already in view.

The evaluation clock is the alert's ``triggered_at``. A payment counts only
when its event time is at or before that instant and inside the requested
lookback. ``ingested_at`` and ``booked_at`` are not read. On a batch-loaded
history those clocks say when the row was stored, not when a source could
have delivered it, so this calculation is an event-time reconstruction.
A later operational cut can also require the row to have been stored before
the decision. That cut is not applied here.

Amounts are summed only in the account currency. A payment in another
currency is counted, and it is not converted. A missing exchange rate is
left missing. An amount statistic is omitted when the account has no
same-currency payment in that window. A count of zero means no matching
payment was visible. ``history_covers_*`` says whether the account was open
for the whole window. A zero beside a false history flag is not evidence
that the account was inactive for that window.

Scenario notes, evaluation labels, case dispositions, and events after
``triggered_at`` are not inputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from uuid import UUID

FEATURE_SET_NAME = "alert_behavior"
FEATURE_SET_VERSION = "1"

WINDOW_1H = timedelta(hours=1)
WINDOW_24H = timedelta(hours=24)
WINDOW_7D = timedelta(days=7)
WINDOW_30D = timedelta(days=30)
_SCALE = Decimal("0.00000001")
_CHANNELS = ("card", "wire", "ach", "wallet")

# Names that are present on every snapshot for an alert with an account.
# Conditional names are documented on the functions that emit them.
CORE_FEATURE_NAMES = (
    "evaluation_event_time",
    "availability_basis",
    "amount_currency",
    "rule_code",
    "rule_version",
    "evidence_count",
    "account_opened_at_known",
    "history_covers_1h",
    "history_covers_24h",
    "history_covers_7d",
    "history_covers_30d",
    "payment_count_1h",
    "payment_count_24h",
    "payment_count_7d",
    "payment_count_30d",
    "debit_count_1h",
    "debit_count_24h",
    "debit_count_7d",
    "debit_count_30d",
    "credit_count_1h",
    "credit_count_24h",
    "credit_count_7d",
    "credit_count_30d",
    "same_currency_count_1h",
    "same_currency_count_24h",
    "same_currency_count_7d",
    "same_currency_count_30d",
    "other_currency_count_24h",
    "other_currency_count_30d",
    "distinct_currency_count_30d",
    "distinct_counterparty_count_24h",
    "distinct_counterparty_count_30d",
    "distinct_channel_count_7d",
    "card_count_24h",
    "wire_count_24h",
    "ach_count_24h",
    "wallet_count_24h",
)


@dataclass(frozen=True)
class PaymentView:
    """One stored payment as this feature set is allowed to see it."""

    transaction_id: UUID
    account_id: UUID
    event_time: datetime
    direction: str
    amount: Decimal
    currency_code: str
    payment_channel: str
    counterparty_reference: str | None


@dataclass(frozen=True)
class AlertView:
    """The alert fields this feature set is allowed to read."""

    alert_id: UUID
    account_id: UUID
    customer_id: UUID
    triggered_at: datetime
    window_started_at: datetime | None
    window_ended_at: datetime | None
    rule_code: str
    rule_version: int
    account_opened_at: datetime | None
    account_currency: str
    evidence_count: int
    triggering_transaction_id: UUID | None


@dataclass(frozen=True)
class FeatureValue:
    """One feature ready to store. Exactly one value field is set."""

    name: str
    value_type: str
    numeric: Decimal | None = None
    text: str | None = None
    boolean: bool | None = None
    timestamp: datetime | None = None


@dataclass(frozen=True)
class FeatureSnapshot:
    """One alert's feature values and the fingerprint of the inputs."""

    values: tuple[FeatureValue, ...]
    input_fingerprint: str

    def by_name(self) -> dict[str, FeatureValue]:
        return {value.name: value for value in self.values}


def compute_snapshot(alert: AlertView, payments: tuple[PaymentView, ...] | list[PaymentView]) -> FeatureSnapshot:
    """Build one snapshot. Payments after the alert, or on another account, are ignored."""

    _require_aware(alert.triggered_at, "triggered_at")
    visible = tuple(sorted(_visible(alert, payments), key=lambda payment: (payment.event_time, payment.transaction_id)))
    values = _values(alert, visible)
    return FeatureSnapshot(values=tuple(values), input_fingerprint=_fingerprint(alert, visible))


def _visible(alert: AlertView, payments: list[PaymentView] | tuple[PaymentView, ...]) -> list[PaymentView]:
    start = alert.triggered_at - WINDOW_30D
    chosen = []
    for payment in payments:
        _require_aware(payment.event_time, "event_time")
        if payment.account_id != alert.account_id:
            continue
        if payment.event_time < start or payment.event_time > alert.triggered_at:
            continue
        chosen.append(payment)
    return chosen


def _values(alert: AlertView, visible: tuple[PaymentView, ...]) -> list[FeatureValue]:
    currency = alert.account_currency.strip().upper()
    covers = {
        "1h": _covers(alert, WINDOW_1H),
        "24h": _covers(alert, WINDOW_24H),
        "7d": _covers(alert, WINDOW_7D),
        "30d": _covers(alert, WINDOW_30D),
    }
    windows = {
        "1h": _in_window(visible, alert.triggered_at, WINDOW_1H),
        "24h": _in_window(visible, alert.triggered_at, WINDOW_24H),
        "7d": _in_window(visible, alert.triggered_at, WINDOW_7D),
        "30d": visible,
    }
    values: list[FeatureValue] = [
        _timestamp("evaluation_event_time", alert.triggered_at),
        _text("availability_basis", "event_timestamp"),
        _text("amount_currency", currency),
        _text("rule_code", alert.rule_code),
        _numeric("rule_version", Decimal(alert.rule_version)),
        _numeric("evidence_count", Decimal(alert.evidence_count)),
        _boolean("account_opened_at_known", alert.account_opened_at is not None and alert.account_opened_at <= alert.triggered_at),
        _boolean("history_covers_1h", covers["1h"]),
        _boolean("history_covers_24h", covers["24h"]),
        _boolean("history_covers_7d", covers["7d"]),
        _boolean("history_covers_30d", covers["30d"]),
    ]
    if alert.window_started_at is not None and alert.window_ended_at is not None:
        seconds = Decimal(str((alert.window_ended_at - alert.window_started_at).total_seconds()))
        values.append(_numeric("alert_window_seconds", seconds))
    if alert.account_opened_at is not None and alert.account_opened_at <= alert.triggered_at:
        age = Decimal(str((alert.triggered_at - alert.account_opened_at).total_seconds()))
        values.append(_numeric("account_age_seconds", age))

    for label, window_payments in windows.items():
        values.append(_numeric(f"payment_count_{label}", Decimal(len(window_payments))))
        values.append(_numeric(f"debit_count_{label}", Decimal(_direction_count(window_payments, "debit"))))
        values.append(_numeric(f"credit_count_{label}", Decimal(_direction_count(window_payments, "credit"))))
        values.append(
            _numeric(f"same_currency_count_{label}", Decimal(_currency_count(window_payments, currency)))
        )

    day = windows["24h"]
    month = windows["30d"]
    week = windows["7d"]
    values.append(_numeric("other_currency_count_24h", Decimal(_other_currency_count(day, currency))))
    values.append(_numeric("other_currency_count_30d", Decimal(_other_currency_count(month, currency))))
    values.append(_numeric("distinct_currency_count_30d", Decimal(len({_code(payment) for payment in month}))))
    values.append(_numeric("distinct_counterparty_count_24h", Decimal(len(_counterparties(day)))))
    values.append(_numeric("distinct_counterparty_count_30d", Decimal(len(_counterparties(month)))))
    values.append(_numeric("distinct_channel_count_7d", Decimal(len({payment.payment_channel for payment in week}))))
    for channel in _CHANNELS:
        count = sum(1 for payment in day if payment.payment_channel == channel)
        values.append(_numeric(f"{channel}_count_24h", Decimal(count)))

    _append_amount(values, "same_currency_debit_sum_24h", _sum(day, currency, "debit"))
    _append_amount(values, "same_currency_debit_sum_30d", _sum(month, currency, "debit"))
    _append_amount(values, "same_currency_credit_sum_24h", _sum(day, currency, "credit"))
    _append_amount(values, "same_currency_debit_max_24h", _max(day, currency, "debit"))
    _append_amount(values, "same_currency_debit_mean_30d", _mean(month, currency, "debit"))
    _append_rate(
        values,
        "debit_hourly_rate_1h_vs_prior_23h",
        recent=_direction_count(windows["1h"], "debit"),
        prior=_direction_count(_prior(visible, alert.triggered_at, WINDOW_24H, WINDOW_1H), "debit"),
        prior_units=Decimal(23),
        covered=covers["24h"],
    )
    _append_rate(
        values,
        "debit_daily_rate_24h_vs_prior_6d",
        recent=_direction_count(day, "debit"),
        prior=_direction_count(_prior(visible, alert.triggered_at, WINDOW_7D, WINDOW_24H), "debit"),
        prior_units=Decimal(6),
        covered=covers["7d"],
    )
    if covers["30d"]:
        values.append(_numeric("new_counterparty_count_24h", Decimal(_new_counterparties(visible, alert.triggered_at))))
    previous = [payment.event_time for payment in visible if payment.event_time < alert.triggered_at]
    if previous:
        gap = Decimal(str((alert.triggered_at - max(previous)).total_seconds()))
        values.append(_numeric("seconds_since_previous_payment", gap))
    triggering = _triggering(alert, visible)
    if triggering is not None and _code(triggering) == currency:
        values.append(_numeric("triggering_amount", _money(triggering.amount)))
    if triggering is not None and triggering.counterparty_reference:
        prior_same = sum(
            1
            for payment in month
            if payment.counterparty_reference == triggering.counterparty_reference
            and payment.event_time < triggering.event_time
        )
        values.append(_numeric("triggering_counterparty_prior_count_30d", Decimal(prior_same)))
    values.sort(key=lambda item: item.name)
    return values


def _covers(alert: AlertView, window: timedelta) -> bool:
    opened = alert.account_opened_at
    return opened is not None and opened <= alert.triggered_at - window


def _in_window(payments: tuple[PaymentView, ...], end: datetime, window: timedelta) -> tuple[PaymentView, ...]:
    start = end - window
    return tuple(payment for payment in payments if start <= payment.event_time <= end)


def _prior(payments: tuple[PaymentView, ...], end: datetime, outer: timedelta, inner: timedelta) -> tuple[PaymentView, ...]:
    """Payments in the outer window and strictly before the inner window."""

    return tuple(
        payment
        for payment in payments
        if end - outer <= payment.event_time < end - inner
    )


def _direction_count(payments: tuple[PaymentView, ...], direction: str) -> int:
    return sum(1 for payment in payments if payment.direction == direction)


def _currency_count(payments: tuple[PaymentView, ...], currency: str) -> int:
    return sum(1 for payment in payments if _code(payment) == currency)


def _other_currency_count(payments: tuple[PaymentView, ...], currency: str) -> int:
    return sum(1 for payment in payments if _code(payment) != currency)


def _code(payment: PaymentView) -> str:
    return payment.currency_code.strip().upper()


def _counterparties(payments: tuple[PaymentView, ...]) -> set[str]:
    return {payment.counterparty_reference for payment in payments if payment.counterparty_reference}


def _same(payments: tuple[PaymentView, ...], currency: str, direction: str) -> list[PaymentView]:
    return [
        payment
        for payment in payments
        if payment.direction == direction and _code(payment) == currency
    ]


def _sum(payments: tuple[PaymentView, ...], currency: str, direction: str) -> Decimal | None:
    chosen = _same(payments, currency, direction)
    if not chosen:
        return None
    return _money(sum((payment.amount for payment in chosen), Decimal("0")))


def _max(payments: tuple[PaymentView, ...], currency: str, direction: str) -> Decimal | None:
    chosen = _same(payments, currency, direction)
    if not chosen:
        return None
    return _money(max(payment.amount for payment in chosen))


def _mean(payments: tuple[PaymentView, ...], currency: str, direction: str) -> Decimal | None:
    chosen = _same(payments, currency, direction)
    if not chosen:
        return None
    total = sum((payment.amount for payment in chosen), Decimal("0"))
    return _money(total / Decimal(len(chosen)))


def _append_amount(values: list[FeatureValue], name: str, amount: Decimal | None) -> None:
    if amount is not None:
        values.append(_numeric(name, amount))


def _append_rate(
    values: list[FeatureValue],
    name: str,
    *,
    recent: int,
    prior: int,
    prior_units: Decimal,
    covered: bool,
) -> None:
    """Recent rate divided by the rate in the preceding slice.

    The feature is omitted when the account was not open for the whole
    baseline, or when that baseline contains no payments. A zero baseline is
    not stored as an infinite ratio.
    """

    if not covered or prior <= 0:
        return
    ratio = (Decimal(recent) * prior_units) / Decimal(prior)
    values.append(_numeric(name, ratio.quantize(_SCALE, rounding=ROUND_HALF_EVEN)))


def _new_counterparties(payments: tuple[PaymentView, ...], end: datetime) -> int:
    """Counterparties first seen inside 24 hours, within the 30-day lookback.

    This is not a lifetime check. Activity before the lookback is outside
    this feature set.
    """

    earliest: dict[str, datetime] = {}
    for payment in payments:
        reference = payment.counterparty_reference
        if reference is None:
            continue
        current = earliest.get(reference)
        if current is None or payment.event_time < current:
            earliest[reference] = payment.event_time
    recent_start = end - WINDOW_24H
    return sum(1 for moment in earliest.values() if moment >= recent_start)


def _triggering(alert: AlertView, payments: tuple[PaymentView, ...]) -> PaymentView | None:
    if alert.triggering_transaction_id is None:
        return None
    for payment in payments:
        if payment.transaction_id == alert.triggering_transaction_id:
            return payment
    return None


def _fingerprint(alert: AlertView, visible: tuple[PaymentView, ...]) -> str:
    lines = [
        FEATURE_SET_NAME,
        FEATURE_SET_VERSION,
        str(alert.alert_id),
        str(alert.account_id),
        alert.triggered_at.isoformat(),
        "" if alert.account_opened_at is None else alert.account_opened_at.isoformat(),
        alert.account_currency.strip().upper(),
        alert.rule_code,
        str(alert.rule_version),
        "" if alert.window_started_at is None else alert.window_started_at.isoformat(),
        "" if alert.window_ended_at is None else alert.window_ended_at.isoformat(),
        str(alert.evidence_count),
        "" if alert.triggering_transaction_id is None else str(alert.triggering_transaction_id),
    ]
    for payment in visible:
        lines.append(
            "|".join(
                (
                    str(payment.transaction_id),
                    payment.event_time.isoformat(),
                    payment.direction,
                    format(payment.amount, "f"),
                    payment.currency_code.strip().upper(),
                    payment.payment_channel,
                    payment.counterparty_reference or "",
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _numeric(name: str, value: Decimal) -> FeatureValue:
    return FeatureValue(name=name, value_type="numeric", numeric=value)


def _boolean(name: str, value: bool) -> FeatureValue:
    return FeatureValue(name=name, value_type="boolean", boolean=value)


def _text(name: str, value: str) -> FeatureValue:
    return FeatureValue(name=name, value_type="text", text=value)


def _timestamp(name: str, value: datetime) -> FeatureValue:
    return FeatureValue(name=name, value_type="timestamp", timestamp=value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{label} must include a time zone.")

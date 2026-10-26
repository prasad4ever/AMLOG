"""R002, rapid funds turnover.

The threshold lives in the rule version's parameter document. This module
does not read scenario notes, labels, or later events.

An alert compares same-currency amounts. It does not prove that the same
funds were transferred. Outgoing amount is allocated to the oldest open
qualifying incoming payment first, and each unit of outgoing amount is
allocated once. Allocation stops at the incoming amount. The alert opens
when a later outgoing payment makes the allocated total reach the ratio,
not when the incoming payment arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping
from uuid import UUID

R002_CODE = "R002"
R002_VERSION = 1
R002_NAME = "Rapid funds turnover"
R002_SEVERITY = "medium"
R002_AUTHORED_BY = "amlog.monitoring"
R002_DESCRIPTION = (
    "Opens one alert when later outgoing payments in the same currency "
    "are allocated to a qualifying incoming payment and reach the configured "
    "ratio inside the event-time window. The comparison is an inference from "
    "amounts. It is not proof that the same funds were transferred. Version 1 "
    "is for historical replay. It is not live monitoring."
)
R002_EFFECTIVE_FROM = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
R002_PARAMETERS = {
    "minimum_incoming_amount": "500",
    "outgoing_ratio": "0.80",
    "window_seconds": 86400,
    "amount_basis": "transaction_currency",
}

_PARAMETER_KEYS = frozenset(R002_PARAMETERS)


@dataclass(frozen=True)
class TurnoverParameters:
    """The threshold a specific R002 version asked the engine to apply."""

    minimum_incoming_amount: Decimal
    outgoing_ratio: Decimal
    window: timedelta
    amount_basis: str

    @property
    def window_seconds(self) -> int:
        return int(self.window.total_seconds())


@dataclass(frozen=True)
class TurnoverTransaction:
    """One stored payment as R002 is allowed to see it."""

    transaction_id: UUID
    account_id: UUID
    customer_id: UUID
    direction: str
    event_time: datetime
    amount: Decimal
    currency_code: str


@dataclass(frozen=True)
class AllocatedOutgoing:
    """The part of one outgoing payment matched to one incoming payment."""

    transaction: TurnoverTransaction
    allocated_amount: Decimal


@dataclass(frozen=True)
class TurnoverEpisode:
    """One incoming payment whose allocated outgoing amount reached the ratio.

    ``incoming`` is the baseline. ``outgoing`` contains only the payments
    allocated up to the moment the ratio was reached. ``triggering`` is the
    outgoing payment being evaluated at that moment.
    """

    account_id: UUID
    customer_id: UUID
    incoming: TurnoverTransaction
    outgoing: tuple[AllocatedOutgoing, ...]
    triggering: TurnoverTransaction
    allocated_outgoing: Decimal

    @property
    def window_started_at(self) -> datetime:
        return self.incoming.event_time

    @property
    def window_ended_at(self) -> datetime:
        return self.triggering.event_time


@dataclass
class _OpenReceipt:
    credit: TurnoverTransaction
    remaining: Decimal
    allocated: Decimal = Decimal("0")
    parts: list[AllocatedOutgoing] = field(default_factory=list)
    alerted: bool = False


def parse_turnover_parameters(document: Mapping[str, object]) -> TurnoverParameters:
    """Read an R002 parameter document. Missing or extra keys fail."""

    if set(document) != _PARAMETER_KEYS:
        raise ValueError(
            "turnover parameter document must contain minimum_incoming_amount, "
            "outgoing_ratio, window_seconds, and amount_basis"
        )
    minimum = _decimal_parameter(document["minimum_incoming_amount"], "minimum_incoming_amount")
    ratio = _decimal_parameter(document["outgoing_ratio"], "outgoing_ratio")
    window_seconds = document["window_seconds"]
    amount_basis = document["amount_basis"]
    if minimum <= 0:
        raise ValueError("minimum_incoming_amount must be positive")
    if ratio <= 0 or ratio > 1:
        raise ValueError("outgoing_ratio must be greater than zero and no more than one")
    if type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("window_seconds must be a positive whole number")
    if amount_basis != "transaction_currency":
        raise ValueError(
            "amount_basis must be transaction_currency. "
            "This version does not convert currencies."
        )
    return TurnoverParameters(
        minimum_incoming_amount=minimum,
        outgoing_ratio=ratio,
        window=timedelta(seconds=window_seconds),
        amount_basis=amount_basis,
    )


def find_turnover_episodes(
    transactions: tuple[TurnoverTransaction, ...] | list[TurnoverTransaction],
    parameters: TurnoverParameters,
) -> tuple[TurnoverEpisode, ...]:
    """Find one episode per qualifying incoming payment.

    Transactions must already be ordered by account, event time, and
    transaction id. A debit can be allocated only to an earlier incoming
    payment in the same currency whose window still contains that debit.
    """

    if parameters.window <= timedelta(0) or parameters.amount_basis != "transaction_currency":
        raise ValueError("turnover parameters are not usable")
    ordered = tuple(transactions)
    _require_event_order(ordered)
    episodes: list[TurnoverEpisode] = []
    for account_rows in _group_by_account(ordered):
        episodes.extend(_episodes_for_account(account_rows, parameters))
    return tuple(episodes)


def _decimal_parameter(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be an exact decimal")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{name} must be an exact decimal") from exc
        if not parsed.is_finite():
            raise ValueError(f"{name} must be an exact decimal")
        return parsed
    raise ValueError(f"{name} must be an exact decimal")


def _require_event_order(transactions: tuple[TurnoverTransaction, ...]) -> None:
    for earlier, later in zip(transactions, transactions[1:], strict=False):
        if earlier.event_time.tzinfo is None or later.event_time.tzinfo is None:
            raise ValueError("event time must carry a timezone")
        if earlier.account_id != later.account_id:
            if earlier.account_id > later.account_id:
                raise ValueError(
                    "transactions must be ordered by account and then event time"
                )
            continue
        if (later.event_time, later.transaction_id) < (
            earlier.event_time,
            earlier.transaction_id,
        ):
            raise ValueError(
                "transactions must be ordered by account and then event time"
            )


def _group_by_account(
    transactions: tuple[TurnoverTransaction, ...],
) -> tuple[tuple[TurnoverTransaction, ...], ...]:
    groups: list[tuple[TurnoverTransaction, ...]] = []
    current: list[TurnoverTransaction] = []
    current_account: UUID | None = None
    for transaction in transactions:
        if transaction.event_time.tzinfo is None:
            raise ValueError("event time must carry a timezone")
        if transaction.amount < 0:
            raise ValueError("transaction amount cannot be negative")
        if current_account != transaction.account_id:
            if current:
                groups.append(tuple(current))
            current = [transaction]
            current_account = transaction.account_id
        else:
            current.append(transaction)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _episodes_for_account(
    transactions: tuple[TurnoverTransaction, ...],
    parameters: TurnoverParameters,
) -> tuple[TurnoverEpisode, ...]:
    receipts: list[_OpenReceipt] = []
    episodes: list[TurnoverEpisode] = []
    customer_id = transactions[0].customer_id
    for transaction in transactions:
        if transaction.customer_id != customer_id:
            raise ValueError("one account produced two customer ids")
        receipts = [
            receipt
            for receipt in receipts
            if transaction.event_time <= receipt.credit.event_time + parameters.window
        ]
        if transaction.direction == "credit":
            if transaction.amount >= parameters.minimum_incoming_amount:
                receipts.append(
                    _OpenReceipt(credit=transaction, remaining=transaction.amount)
                )
            continue
        if transaction.direction != "debit":
            continue
        _allocate_debit(transaction, receipts, parameters, episodes)
    return tuple(episodes)


def _allocate_debit(
    debit: TurnoverTransaction,
    receipts: list[_OpenReceipt],
    parameters: TurnoverParameters,
    episodes: list[TurnoverEpisode],
) -> None:
    remaining = debit.amount
    for receipt in receipts:
        if remaining <= 0:
            return
        if receipt.remaining <= 0 or receipt.credit.currency_code != debit.currency_code:
            continue
        if debit.event_time <= receipt.credit.event_time:
            continue
        if debit.event_time > receipt.credit.event_time + parameters.window:
            continue
        take = min(remaining, receipt.remaining)
        receipt.remaining -= take
        remaining -= take
        if receipt.alerted:
            continue
        receipt.allocated += take
        receipt.parts.append(AllocatedOutgoing(debit, take))
        threshold = receipt.credit.amount * parameters.outgoing_ratio
        if receipt.allocated >= threshold:
            episodes.append(
                TurnoverEpisode(
                    account_id=debit.account_id,
                    customer_id=debit.customer_id,
                    incoming=receipt.credit,
                    outgoing=tuple(receipt.parts),
                    triggering=debit,
                    allocated_outgoing=receipt.allocated,
                )
            )
            receipt.alerted = True

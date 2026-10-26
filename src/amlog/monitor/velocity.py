"""R001, outgoing transaction velocity.

The numbers that decide an alert live in the rule version's parameter
document. This module applies whatever window and count it is given. It does
not read scenario notes, labels, or later events.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping
from uuid import UUID

R001_CODE = "R001"
R001_VERSION = 1
R001_NAME = "Outgoing transaction velocity"
R001_SEVERITY = "medium"
R001_AUTHORED_BY = "amlog.monitoring"
R001_DESCRIPTION = (
    "Opens one alert when an account has at least the configured number of "
    "outgoing transactions inside a rolling event-time window. Version 1 is "
    "for historical replay of stored transactions. It is not live monitoring."
)
R001_EFFECTIVE_FROM = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
R001_PARAMETERS = {
    "direction": "debit",
    "window_seconds": 3600,
    "minimum_count": 6,
}

_PARAMETER_KEYS = frozenset(R001_PARAMETERS)


@dataclass(frozen=True)
class VelocityParameters:
    """The threshold a specific rule version asked the engine to apply."""

    direction: str
    window: timedelta
    minimum_count: int

    @property
    def window_seconds(self) -> int:
        return int(self.window.total_seconds())


@dataclass(frozen=True)
class ObservedTransaction:
    """One stored payment as the rule is allowed to see it."""

    transaction_id: UUID
    account_id: UUID
    customer_id: UUID
    direction: str
    event_time: datetime


@dataclass(frozen=True)
class VelocityBurst:
    """The first moment a rolling window reaches the threshold.

    ``members`` are the matching transactions inside that window, oldest
    first. ``triggering`` is the transaction being evaluated when the count
    first reached the threshold. Later activity in the same burst is not a
    second finding and is not added to this set.
    """

    account_id: UUID
    customer_id: UUID
    members: tuple[ObservedTransaction, ...]
    triggering: ObservedTransaction

    @property
    def window_started_at(self) -> datetime:
        return self.members[0].event_time

    @property
    def window_ended_at(self) -> datetime:
        return self.triggering.event_time


def parse_velocity_parameters(document: Mapping[str, object]) -> VelocityParameters:
    """Read a rule version's parameter document. Missing or extra keys fail."""

    if set(document) != _PARAMETER_KEYS:
        raise ValueError(
            "velocity parameter document must contain direction, window_seconds, "
            "and minimum_count"
        )
    direction = document["direction"]
    window_seconds = document["window_seconds"]
    minimum_count = document["minimum_count"]
    if direction not in ("debit", "credit"):
        raise ValueError("velocity direction must be debit or credit")
    if type(window_seconds) is not int or isinstance(window_seconds, bool):
        raise ValueError("window_seconds must be a positive whole number")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be a positive whole number")
    if type(minimum_count) is not int or isinstance(minimum_count, bool):
        raise ValueError("minimum_count must be a positive whole number")
    if minimum_count < 1:
        raise ValueError("minimum_count must be a positive whole number")
    return VelocityParameters(
        direction=direction,
        window=timedelta(seconds=window_seconds),
        minimum_count=minimum_count,
    )


def find_velocity_bursts(
    transactions: tuple[ObservedTransaction, ...] | list[ObservedTransaction],
    parameters: VelocityParameters,
) -> tuple[VelocityBurst, ...]:
    """Find one burst per account from transactions ordered by event time.

    A candidate counts when its event time is inside the closed interval that
    ends at the transaction being evaluated and starts ``window`` earlier.
    Transactions after that instant are not visible. A burst stays open while
    each later matching transaction still meets the count. The next time the
    count is reached, after it has fallen below the threshold, is a new burst.
    """

    if parameters.window <= timedelta(0) or parameters.minimum_count < 1:
        raise ValueError("velocity parameters are not usable")
    ordered = tuple(transactions)
    _require_event_order(ordered)
    bursts: list[VelocityBurst] = []
    for account_rows in _group_by_account(ordered):
        bursts.extend(_bursts_for_account(account_rows, parameters))
    return tuple(bursts)


def _require_event_order(transactions: tuple[ObservedTransaction, ...]) -> None:
    for earlier, later in zip(transactions, transactions[1:], strict=False):
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
        if later.event_time.tzinfo is None or earlier.event_time.tzinfo is None:
            raise ValueError("event time must carry a timezone")


def _group_by_account(
    transactions: tuple[ObservedTransaction, ...],
) -> tuple[tuple[ObservedTransaction, ...], ...]:
    groups: list[tuple[ObservedTransaction, ...]] = []
    current: list[ObservedTransaction] = []
    current_account: UUID | None = None
    for transaction in transactions:
        if transaction.event_time.tzinfo is None:
            raise ValueError("event time must carry a timezone")
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


def _bursts_for_account(
    transactions: tuple[ObservedTransaction, ...],
    parameters: VelocityParameters,
) -> tuple[VelocityBurst, ...]:
    window: deque[ObservedTransaction] = deque()
    bursts: list[VelocityBurst] = []
    burst_open = False
    customer_id = transactions[0].customer_id
    for transaction in transactions:
        if transaction.customer_id != customer_id:
            raise ValueError("one account produced two customer ids")
        if transaction.direction != parameters.direction:
            continue
        cutoff = transaction.event_time - parameters.window
        while window and window[0].event_time < cutoff:
            window.popleft()
        window.append(transaction)
        if len(window) >= parameters.minimum_count:
            if not burst_open:
                members = tuple(window)
                bursts.append(
                    VelocityBurst(
                        account_id=transaction.account_id,
                        customer_id=customer_id,
                        members=members,
                        triggering=transaction,
                    )
                )
                burst_open = True
        else:
            burst_open = False
    return tuple(bursts)

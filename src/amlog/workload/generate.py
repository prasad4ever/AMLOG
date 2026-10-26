"""Deterministic fictional payment histories.

Parties and events are produced from a seed and a population id. The same
pair produces the same stream. A different seed or population id produces
an independent stream. Events are yielded one at a time so the caller can
load them without keeping the whole history in memory.

Nothing in this module is a fraud judgment. Scenario codes describe the
shape of the fictional activity. ``rapid_movement`` is a receipt on one
account and a later outgoing payment on another account. The
``same_account_*`` codes, ``supplier_follow_on``, ``recurring_after_receipt``,
and ``cross_currency_outgoing`` keep the receipt and the later payment on
one account. ``same_account_split`` uses several outgoing payments that
together cross the turnover ratio, and ``same_account_partial`` stays below
it. ``velocity_burst`` places enough card payments inside one hour to meet
the outgoing-velocity rule. ``velocity_short`` and ``velocity_wide`` are the
nearby shapes that do not. An episode is started only when every payment in
it fits before the history ends. A monitoring rule does not receive these
codes.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from amlog.ingest.contract import EVENT_TYPE, SCHEMA_VERSION_2

POPULATION_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
ANNOTATION_NOTE = (
    "Synthetic workload scenario for development. "
    "This is not a fraud determination, a monitoring result, or a model label."
)

PROFILES = (
    "consumer_regular",
    "salaried",
    "small_business",
    "merchant",
    "cross_border",
    "active_cardholder",
    "marketplace",
)

SCENARIO_CODES = (
    "regular_spend",
    "salary",
    "recurring_payment",
    "merchant_sale",
    "business_payment",
    "cross_border",
    "seasonal_spend",
    "high_frequency",
    "rapid_movement",
    "unusual_amount",
    "new_counterparty",
    "same_account_below",
    "same_account_exact",
    "same_account_above",
    "same_account_late",
    "supplier_follow_on",
    "recurring_after_receipt",
    "cross_currency_outgoing",
    "same_account_split",
    "same_account_partial",
    "velocity_burst",
    "velocity_short",
    "velocity_wide",
)

_REQUIRED = {
    "consumer_regular": (
        "regular_spend",
        "seasonal_spend",
        "new_counterparty",
        "cross_currency_outgoing",
        "same_account_partial",
    ),
    "salaried": (
        "salary",
        "recurring_payment",
        "regular_spend",
        "same_account_late",
        "velocity_short",
    ),
    "small_business": (
        "business_payment",
        "cross_border",
        "same_account_exact",
        "same_account_above",
        "supplier_follow_on",
    ),
    "merchant": (
        "merchant_sale",
        "high_frequency",
        "same_account_below",
        "recurring_after_receipt",
        "same_account_split",
    ),
    "cross_border": (
        "cross_border",
        "regular_spend",
        "same_account_exact",
        "velocity_wide",
    ),
    "active_cardholder": (
        "high_frequency",
        "unusual_amount",
        "same_account_above",
        "velocity_burst",
    ),
    "marketplace": ("rapid_movement",),
}

_POOL = {
    "consumer_regular": (
        "regular_spend",
        "seasonal_spend",
        "new_counterparty",
        "recurring_payment",
        "cross_currency_outgoing",
        "same_account_partial",
    ),
    "salaried": ("salary", "regular_spend", "recurring_payment", "velocity_short"),
    "small_business": (
        "business_payment",
        "cross_border",
        "recurring_payment",
        "same_account_exact",
        "supplier_follow_on",
        "same_account_split",
    ),
    "merchant": (
        "merchant_sale",
        "high_frequency",
        "same_account_below",
        "recurring_after_receipt",
        "same_account_split",
    ),
    "cross_border": (
        "cross_border",
        "regular_spend",
        "business_payment",
        "same_account_exact",
        "velocity_wide",
    ),
    "active_cardholder": (
        "high_frequency",
        "regular_spend",
        "unusual_amount",
        "same_account_above",
        "velocity_burst",
    ),
    "marketplace": ("rapid_movement", "merchant_sale", "regular_spend"),
}

_RAILS = {
    "consumer_regular": ("wallet", "bank-transfer"),
    "salaried": ("bank-transfer", "wallet"),
    "small_business": ("bank-transfer",),
    "merchant": ("merchant-payment",),
    "cross_border": ("bank-transfer",),
    "active_cardholder": ("wallet",),
    "marketplace": ("wallet", "bank-transfer"),
}

_CURRENCIES = ("USD", "EUR", "GBP", "CAD")
_RESIDENCY = {"USD": "US", "EUR": "DE", "GBP": "GB", "CAD": "CA"}
_USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "CAD": Decimal("0.74"),
}
_MONEY = Decimal("0.01")
_RATE = Decimal("0.00000001")


@dataclass(frozen=True)
class _PairShape:
    """A receipt followed by one outgoing payment on the same account.

    ``quiet_after_hours`` keeps the next payment outside the receipt's
    24-hour comparison window when this shape is not meant to qualify.
    These fields describe the fictional payments. They are not a rule result.
    """

    ratio: Decimal
    hours: int
    outgoing_role: str
    quiet_after_hours: int


_PAIR_SHAPES = {
    "same_account_below": _PairShape(Decimal("0.50"), 12, "supplier", 13),
    "same_account_exact": _PairShape(Decimal("0.80"), 24, "supplier", 1),
    "same_account_above": _PairShape(Decimal("0.95"), 6, "supplier", 19),
    "same_account_late": _PairShape(Decimal("0.95"), 36, "payee", 0),
    "supplier_follow_on": _PairShape(Decimal("0.30"), 18, "supplier", 7),
    "recurring_after_receipt": _PairShape(Decimal("0.10"), 8, "utility", 17),
    "cross_currency_outgoing": _PairShape(Decimal("0.90"), 4, "foreign", 21),
}

_RECEIPT_CENTS = {
    "same_account_below": (80_000, 250_000),
    "same_account_exact": (100_000, 500_000),
    "same_account_above": (200_000, 800_000),
    "same_account_late": (150_000, 600_000),
    "supplier_follow_on": (300_000, 1_200_000),
    "recurring_after_receipt": (200_000, 700_000),
    "cross_currency_outgoing": (100_000, 400_000),
    "same_account_split": (150_000, 900_000),
    "same_account_partial": (120_000, 600_000),
}


@dataclass(frozen=True)
class _Leg:
    """One payment inside a planned episode. The offset is from the first leg."""

    direction: str
    ratio: Decimal | None
    offset: timedelta
    role: str


_SPLIT_PLANS = (
    ((Decimal("0.20"), 2), (Decimal("0.25"), 6), (Decimal("0.40"), 14)),
    ((Decimal("0.30"), 4), (Decimal("0.20"), 9), (Decimal("0.35"), 16)),
    ((Decimal("0.15"), 3), (Decimal("0.30"), 8), (Decimal("0.40"), 18)),
)
_BURST_PLANS = ((6, 4), (7, 5), (8, 3), (6, 9))
_PAYEE_NAMES = ("Fieldnote Supply", "Quarry Tools", "Pike Street Books", "Redbird Provisions")


def _legs(code: str, party_index: int) -> tuple[_Leg, ...] | None:
    """The payments that must be emitted together, or None for a single payment."""

    if code == "rapid_movement":
        return (
            _Leg("credit", None, timedelta(0), "platform"),
            _Leg("debit", Decimal("0.98"), timedelta(hours=20), "supplier"),
        )
    if code in _PAIR_SHAPES:
        shape = _PAIR_SHAPES[code]
        return (
            _Leg("credit", None, timedelta(0), "receipt"),
            _Leg("debit", shape.ratio, timedelta(hours=shape.hours), shape.outgoing_role),
        )
    if code == "same_account_split":
        plan = _SPLIT_PLANS[party_index % len(_SPLIT_PLANS)]
        return (
            _Leg("credit", None, timedelta(0), "receipt"),
            *(
                _Leg("debit", ratio, timedelta(hours=hours), "supplier")
                for ratio, hours in plan
            ),
        )
    if code == "same_account_partial":
        return (
            _Leg("credit", None, timedelta(0), "receipt"),
            _Leg("debit", Decimal("0.20"), timedelta(hours=3), "supplier"),
            _Leg("debit", Decimal("0.25"), timedelta(hours=10), "utility"),
        )
    if code == "velocity_burst":
        count, minutes = _BURST_PLANS[party_index % len(_BURST_PLANS)]
        return tuple(
            _Leg("debit", None, timedelta(minutes=index * minutes), "card")
            for index in range(count)
        )
    if code == "velocity_short":
        return tuple(
            _Leg("debit", None, timedelta(minutes=index * 6), "card")
            for index in range(5)
        )
    if code == "velocity_wide":
        return tuple(
            _Leg("debit", None, timedelta(minutes=index * 15), "card")
            for index in range(6)
        )
    return None


def _quiet_after(code: str) -> timedelta:
    """Keep the next payment from joining an episode that should stand alone."""

    if code in {"velocity_burst", "velocity_short", "velocity_wide", "rapid_movement"}:
        return timedelta(hours=2)
    if code in _PAIR_SHAPES:
        return timedelta(hours=_PAIR_SHAPES[code].quiet_after_hours)
    if code == "same_account_split":
        return timedelta(hours=12)
    if code == "same_account_partial":
        return timedelta(hours=16)
    return timedelta(0)

_GIVEN = (
    "Avery", "Blair", "Cameron", "Drew", "Eden", "Finley", "Gray", "Harper",
    "Indigo", "Jules", "Kerry", "Logan", "Morgan", "Noah", "Parker", "Quinn",
)
_FAMILY = (
    "Alden", "Brooks", "Carter", "Dalton", "Ellis", "Foster", "Hayes", "Iverson",
    "Keene", "Lane", "Marlow", "Nash", "Owen", "Pryor", "Reed", "Sloane",
)
_ORGANS = (
    "Northline Goods", "Bright Harbor Market", "Cedar Payroll", "Lumen Freight",
    "Fieldnote Supply", "Marrow & Pine", "Oak Ledger Services", "Pike Street Books",
    "Quarry Tools", "Redbird Provisions", "Sable Studio", "Tidepool Coffee",
)


@dataclass(frozen=True)
class WorkloadSpec:
    """Controls one reproducible fictional population.

    ``event_count`` has no fixed ceiling. Callers choose a small count for a
    local check and a larger count only when they want that volume.
    """

    seed: int
    population_id: str
    event_count: int
    history_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)
    history_end: datetime = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
    batch_size: int = 200

    def __post_init__(self) -> None:
        if type(self.seed) is not int or isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer.")
        if not POPULATION_ID.match(self.population_id):
            raise ValueError(
                "population_id must start with a letter and contain only lowercase letters, digits, and hyphens."
            )
        if type(self.event_count) is not int or isinstance(self.event_count, bool) or self.event_count < 1:
            raise ValueError("event_count must be a positive integer.")
        if self.history_start.tzinfo is None or self.history_end.tzinfo is None:
            raise ValueError("History bounds must include a time zone.")
        if self.history_end <= self.history_start:
            raise ValueError("history_end must be later than history_start.")
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise ValueError("batch_size must be a positive integer.")


@dataclass(frozen=True)
class AccountPlan:
    account_reference: str
    source_system: str
    source_account_id: str
    account_type: str
    currency_code: str
    opened_at: datetime


@dataclass(frozen=True)
class PartyPlan:
    index: int
    profile_code: str
    customer_reference: str
    customer_type: str
    legal_name: str
    residency_country_code: str
    onboarded_at: datetime
    currency_code: str
    accounts: tuple[AccountPlan, ...]


@dataclass(frozen=True)
class GeneratedEvent:
    """One payment plus the scenario note that must stay outside the payment."""

    event_time: datetime
    account_opened_at: datetime
    profile_code: str
    scenario_code: str
    payload: dict[str, Any]


def planned_customer_count(event_count: int) -> int:
    """Grow the party count with the event count, always covering every profile."""

    if event_count < 1:
        raise ValueError("event_count must be a positive integer.")
    return max(len(PROFILES), min(event_count, max(len(PROFILES), event_count // 25)))


def build_parties(spec: WorkloadSpec) -> tuple[PartyPlan, ...]:
    count = planned_customer_count(spec.event_count)
    quotas = _quotas(spec.event_count, count)
    parties: list[PartyPlan] = []
    for index, quota in enumerate(quotas):
        if quota < 1:
            continue
        profile = PROFILES[index % len(PROFILES)]
        rng = Random(f"{spec.seed}:{spec.population_id}:party:{index}")
        currency = _CURRENCIES[index % len(_CURRENCIES)]
        opened = spec.history_start - timedelta(days=30 + rng.randint(0, 300))
        customer_type = "organization" if profile in {"small_business", "merchant"} else "individual"
        parties.append(
            PartyPlan(
                index=index,
                profile_code=profile,
                customer_reference=f"WL-{spec.population_id}-C{index:04d}",
                customer_type=customer_type,
                legal_name=_legal_name(rng, customer_type),
                residency_country_code=_RESIDENCY[currency],
                onboarded_at=opened,
                currency_code=currency,
                accounts=_accounts(spec, index, profile, currency, opened),
            )
        )
    return tuple(parties)


def iter_events(spec: WorkloadSpec) -> Iterator[GeneratedEvent]:
    """Yield payment events in time order, one at a time."""

    parties = build_parties(spec)
    quotas = _quotas(spec.event_count, len(parties))
    streams = [
        _iter_party(spec, party, quota)
        for party, quota in zip(parties, quotas, strict=True)
        if quota > 0
    ]
    heap: list[tuple[datetime, int, GeneratedEvent]] = []
    for index, stream in enumerate(streams):
        try:
            event = next(stream)
        except StopIteration:
            continue
        heapq.heappush(heap, (event.event_time, index, event))
    while heap:
        _, index, event = heapq.heappop(heap)
        yield event
        try:
            nxt = next(streams[index])
        except StopIteration:
            continue
        heapq.heappush(heap, (nxt.event_time, index, nxt))


def _quotas(event_count: int, parties: int) -> list[int]:
    base, extra = divmod(event_count, parties)
    return [base + (1 if index < extra else 0) for index in range(parties)]


def _accounts(
    spec: WorkloadSpec,
    index: int,
    profile: str,
    currency: str,
    opened: datetime,
) -> tuple[AccountPlan, ...]:
    account_type = {
        "small_business": "operating",
        "merchant": "settlement",
    }.get(profile, "deposit")
    plans: list[AccountPlan] = []
    for rail in _RAILS[profile]:
        plans.append(
            AccountPlan(
                account_reference=f"WL-{spec.population_id}-C{index:04d}-{rail}",
                source_system=rail,
                source_account_id=f"{spec.population_id}-C{index:04d}-{rail}",
                account_type=account_type,
                currency_code=currency,
                opened_at=opened,
            )
        )
    return tuple(plans)


def _iter_party(spec: WorkloadSpec, party: PartyPlan, quota: int) -> Iterator[GeneratedEvent]:
    rng = Random(f"{spec.seed}:{spec.population_id}:events:{party.index}")
    previous_time: datetime | None = None
    previous_code: str | None = None
    previous_amount: Decimal | None = None
    high_frequency_seen = 0
    salary_seen = 0
    required = _REQUIRED[party.profile_code]
    required_cursor = 0
    pending: list[int] = []
    episode_code: str | None = None
    episode_start: datetime | None = None
    episode_receipt: Decimal | None = None
    quiet_until: datetime | None = None
    for sequence in range(quota):
        if pending:
            leg_index = pending.pop(0)
            code = episode_code or "regular_spend"
        else:
            leg_index = 0
            episode_code = None
            episode_start = None
            episode_receipt = None
            if required_cursor < len(required):
                code = required[required_cursor]
                required_cursor += 1
            else:
                code = rng.choice(_POOL[party.profile_code])
            legs = _legs(code, party.index)
            if legs is not None:
                if _episode_fits(
                    spec,
                    previous_time,
                    previous_code,
                    code,
                    sequence,
                    quota,
                    high_frequency_seen,
                    salary_seen,
                    legs,
                    quiet_until,
                ):
                    episode_code = code
                    pending = list(range(1, len(legs)))
                else:
                    code = "regular_spend"
                    legs = None
        legs = _legs(code, party.index) if code != "regular_spend" or leg_index else _legs(code, party.index)
        leg = legs[leg_index] if legs is not None and leg_index < len(legs) else None
        event_time = _leg_time(
            spec,
            sequence,
            quota,
            code,
            previous_time,
            previous_code,
            high_frequency_seen,
            salary_seen,
            leg,
            episode_start,
            quiet_until if leg_index == 0 else None,
        )
        if code == "high_frequency":
            high_frequency_seen += 1
        if code == "salary":
            salary_seen += 1
        amount = _leg_amount(rng, code, leg, episode_receipt, previous_code, previous_amount)
        if leg is not None and leg.direction == "credit":
            episode_receipt = amount
        if leg_index == 0 and leg is not None:
            episode_start = event_time
        account = _leg_account(party, code, sequence, leg_index)
        payload = _payload(
            spec,
            party,
            account,
            sequence,
            code,
            event_time,
            amount,
            bool(leg is not None and leg.direction == "debit" and code == "cross_currency_outgoing"),
            None if leg is None else leg.direction,
            "" if leg is None else leg.role,
        )
        yield GeneratedEvent(
            event_time=event_time,
            account_opened_at=account.opened_at,
            profile_code=party.profile_code,
            scenario_code=code,
            payload=payload,
        )
        if code == "salary":
            quiet_until = event_time + timedelta(hours=25)
        elif leg is not None and not pending and episode_code == code:
            quiet = _quiet_after(code)
            if quiet > timedelta(0):
                quiet_until = event_time + quiet
        previous_time = event_time
        previous_code = code
        previous_amount = amount


def _episode_fits(
    spec: WorkloadSpec,
    previous_time: datetime | None,
    previous_code: str | None,
    code: str,
    sequence: int,
    quota: int,
    high_frequency_seen: int,
    salary_seen: int,
    legs: tuple[_Leg, ...],
    quiet_until: datetime | None,
) -> bool:
    """A planned episode starts only when every leg fits in the quota and the history."""

    if sequence + len(legs) > quota:
        return False
    start = _leg_time(
        spec,
        sequence,
        quota,
        code,
        previous_time,
        previous_code,
        high_frequency_seen,
        salary_seen,
        legs[0],
        None,
        quiet_until,
    )
    finish = start + legs[-1].offset
    return finish <= spec.history_end


def _leg_time(
    spec: WorkloadSpec,
    sequence: int,
    quota: int,
    code: str,
    previous_time: datetime | None,
    previous_code: str | None,
    high_frequency_seen: int,
    salary_seen: int,
    leg: _Leg | None,
    episode_start: datetime | None,
    quiet_until: datetime | None,
) -> datetime:
    if leg is not None and episode_start is not None and leg.offset > timedelta(0):
        candidate = episode_start + leg.offset
    else:
        candidate = _event_time(
            spec,
            sequence,
            quota,
            code if leg is None or leg.offset == timedelta(0) else "regular_spend",
            previous_time,
            None if leg is not None else previous_code,
            high_frequency_seen,
            salary_seen,
        )
        if quiet_until is not None and candidate < quiet_until:
            candidate = quiet_until
        if code in {"velocity_burst", "velocity_short", "velocity_wide"} and previous_time is not None:
            separated = previous_time + timedelta(hours=2)
            if candidate < separated:
                candidate = separated
    return _clamp_time(candidate, spec.history_start, spec.history_end, previous_time)


def _leg_amount(
    rng: Random,
    code: str,
    leg: _Leg | None,
    episode_receipt: Decimal | None,
    previous_code: str | None,
    previous_amount: Decimal | None,
) -> Decimal:
    if leg is not None and leg.ratio is not None and episode_receipt is not None:
        return (episode_receipt * leg.ratio).quantize(_MONEY)
    if leg is not None and leg.direction == "debit" and leg.ratio is None and code.startswith("velocity_"):
        return (Decimal(rng.randint(800, 7500)) / Decimal(100)).quantize(_MONEY)
    if leg is not None and leg.direction == "credit" and code in _RECEIPT_CENTS:
        low, high = _RECEIPT_CENTS[code]
        cents = rng.randint(low, high)
        cents -= cents % 20
        if cents < low:
            cents = low
        return (Decimal(cents) / Decimal(100)).quantize(_MONEY)
    if leg is not None:
        return _amount(rng, code, None, None)
    return _amount(rng, code, previous_code, previous_amount)


def _leg_account(party: PartyPlan, code: str, sequence: int, leg_index: int) -> AccountPlan:
    if code == "rapid_movement":
        rail = "wallet" if leg_index == 0 else "bank-transfer"
        for account in party.accounts:
            if account.source_system == rail:
                return account
        return party.accounts[0]
    if _legs(code, party.index) is not None:
        return _operating_account(party)
    return _account_for(party, code, sequence)


def _scenario_at(profile: str, sequence: int, rng: Random) -> str:
    required = _REQUIRED[profile]
    if sequence < len(required):
        return required[sequence]
    return rng.choice(_POOL[profile])


def _event_time(
    spec: WorkloadSpec,
    sequence: int,
    quota: int,
    code: str,
    previous_time: datetime | None,
    previous_code: str | None,
    high_frequency_seen: int,
    salary_seen: int,
) -> datetime:
    if code in _PAIR_SHAPES and previous_code == code and previous_time is not None:
        candidate = previous_time + timedelta(hours=_PAIR_SHAPES[code].hours)
    elif code == "rapid_movement" and previous_code == "rapid_movement" and previous_time is not None:
        candidate = previous_time + timedelta(hours=20)
    elif code == "salary":
        year = spec.history_start.year
        month = 1 + (salary_seen % 12)
        candidate = datetime(year, month, 15, 9, 0, tzinfo=timezone.utc)
        while previous_time is not None and candidate <= previous_time and candidate < spec.history_end:
            month += 1
            if month == 13:
                month = 1
                year += 1
            candidate = datetime(year, month, 15, 9, 0, tzinfo=timezone.utc)
    elif code == "high_frequency":
        candidate = spec.history_start + timedelta(days=high_frequency_seen, hours=15)
    elif code == "seasonal_spend":
        candidate = datetime(spec.history_start.year, 12, 5, 16, 0, tzinfo=timezone.utc)
        candidate += timedelta(days=sequence % 20)
    else:
        span = spec.history_end - spec.history_start
        candidate = spec.history_start + (span * (sequence + 1) / (quota + 1))
    if (
        previous_code in _PAIR_SHAPES
        and code != previous_code
        and previous_time is not None
        and _PAIR_SHAPES[previous_code].quiet_after_hours > 0
    ):
        earliest = previous_time + timedelta(hours=_PAIR_SHAPES[previous_code].quiet_after_hours)
        if candidate < earliest:
            candidate = earliest
    return _clamp_time(candidate, spec.history_start, spec.history_end, previous_time)


def _clamp_time(
    candidate: datetime,
    start: datetime,
    end: datetime,
    previous_time: datetime | None,
) -> datetime:
    if candidate < start:
        candidate = start
    if candidate > end:
        candidate = end
    if previous_time is not None and candidate <= previous_time:
        # A later payment whose own clock is behind the previous one stays
        # in order, but not one second later. A one-second stack is enough
        # to look like a velocity burst. Two hours keeps it outside R001's window.
        separated = previous_time + timedelta(hours=2)
        if separated <= end:
            candidate = separated
        else:
            candidate = previous_time + timedelta(seconds=1)
            if candidate > end:
                candidate = end
    return candidate


def _amount(
    rng: Random,
    code: str,
    previous_code: str | None,
    previous_amount: Decimal | None,
) -> Decimal:
    if code in _PAIR_SHAPES and previous_code == code and previous_amount is not None:
        return (previous_amount * _PAIR_SHAPES[code].ratio).quantize(_MONEY)
    if code in _RECEIPT_CENTS:
        low, high = _RECEIPT_CENTS[code]
        cents = rng.randint(low, high)
        if code == "same_account_exact":
            cents -= cents % 5
            if cents < low:
                cents = low
        return (Decimal(cents) / Decimal(100)).quantize(_MONEY)
    if code == "rapid_movement" and previous_code == "rapid_movement" and previous_amount is not None:
        return (previous_amount * Decimal("0.98")).quantize(_MONEY)
    ranges = {
        "regular_spend": (500, 12000),
        "salary": (280000, 650000),
        "recurring_payment": (4000, 18000),
        "merchant_sale": (400, 9000),
        "business_payment": (20000, 800000),
        "cross_border": (15000, 400000),
        "seasonal_spend": (2000, 35000),
        "high_frequency": (300, 4500),
        "rapid_movement": (50000, 500000),
        "unusual_amount": (250000, 1200000),
        "new_counterparty": (1500, 25000),
    }
    low, high = ranges[code]
    cents = rng.randint(low, high)
    return (Decimal(cents) / Decimal(100)).quantize(_MONEY)


def _account_for(party: PartyPlan, code: str, sequence: int) -> AccountPlan:
    preferred = {
        "salary": "bank-transfer",
        "recurring_payment": "bank-transfer",
        "business_payment": "bank-transfer",
        "cross_border": "bank-transfer",
        "merchant_sale": "merchant-payment",
        "high_frequency": "wallet",
        "regular_spend": "wallet",
        "seasonal_spend": "wallet",
        "unusual_amount": "wallet",
        "new_counterparty": "wallet",
    }.get(code)
    if code in _PAIR_SHAPES:
        return _operating_account(party)
    if code == "rapid_movement" and sequence % 2 == 1:
        preferred = "bank-transfer"
    elif code == "rapid_movement":
        preferred = "wallet"
    for account in party.accounts:
        if account.source_system == preferred:
            return account
    return party.accounts[0]


def _operating_account(party: PartyPlan) -> AccountPlan:
    """The account a same-account receipt and its outgoing payment share."""

    for rail in ("bank-transfer", "merchant-payment", "wallet"):
        for account in party.accounts:
            if account.source_system == rail:
                return account
    return party.accounts[0]


def _payload(
    spec: WorkloadSpec,
    party: PartyPlan,
    account: AccountPlan,
    sequence: int,
    code: str,
    event_time: datetime,
    amount: Decimal,
    second_leg: bool,
    direction: str | None = None,
    role: str = "",
) -> dict[str, Any]:
    if direction is None:
        direction = "credit" if code in {"salary", "merchant_sale"} else "debit"
        if code == "rapid_movement" and sequence % 2 == 0:
            direction = "credit"
        if code in _PAIR_SHAPES:
            direction = "debit" if second_leg else "credit"
    channel, transaction_type = _channel(code, direction)
    if role == "card" or code.startswith("velocity_"):
        channel, transaction_type = "card", "purchase"
    elif code in _PAIR_SHAPES or code in {"same_account_split", "same_account_partial", "rapid_movement"}:
        if direction == "credit":
            if account.source_system == "wallet":
                channel, transaction_type = "wallet", "transfer"
            elif account.source_system == "merchant-payment":
                channel, transaction_type = "card", "purchase"
            else:
                channel, transaction_type = "ach", "transfer"
        elif role == "utility":
            channel, transaction_type = "ach", "transfer"
        else:
            channel, transaction_type = "wire", "transfer"
    reference, name, country = _counterparty(spec, party, code, sequence, second_leg, role)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_2,
        "event_type": EVENT_TYPE,
        "source_system": account.source_system,
        "source_event_id": f"wl:{spec.population_id}:{party.index:04d}:{sequence:06d}",
        "account_reference": account.account_reference,
        "source_account_id": account.source_account_id,
        "transaction_type": transaction_type,
        "payment_channel": channel,
        "direction": direction,
        "amount": format(amount, "f"),
        "currency_code": account.currency_code,
        "event_timestamp": event_time.isoformat(),
        "booked_at": (event_time + timedelta(minutes=20)).isoformat(),
        "counterparty_reference": reference,
        "counterparty_name": name,
        "correlation_id": str(
            uuid5(
                NAMESPACE_URL,
                f"amlog-workload:{spec.population_id}:{account.source_system}:{party.index}:{sequence}",
            )
        ),
    }
    if country is not None:
        payload["counterparty_country_code"] = country
    if code == "cross_border":
        foreign = _CURRENCIES[(_CURRENCIES.index(account.currency_code) + 1) % len(_CURRENCIES)]
        payload["currency_code"] = foreign
        rate = (_USD_PER_UNIT[foreign] / _USD_PER_UNIT[account.currency_code]).quantize(_RATE)
        payload["fx_rate"] = format(rate, "f")
        payload["base_currency_code"] = account.currency_code
        payload["base_amount"] = format((amount * rate).quantize(_MONEY), "f")
        payload["counterparty_country_code"] = _RESIDENCY[foreign]
        payload["counterparty_institution_reference"] = f"bank-{foreign.lower()}"
    if code == "cross_currency_outgoing" and second_leg:
        foreign = _CURRENCIES[(_CURRENCIES.index(account.currency_code) + 1) % len(_CURRENCIES)]
        payload["currency_code"] = foreign
        rate = (_USD_PER_UNIT[foreign] / _USD_PER_UNIT[account.currency_code]).quantize(_RATE)
        payload["fx_rate"] = format(rate, "f")
        payload["base_currency_code"] = account.currency_code
        payload["base_amount"] = format((amount * rate).quantize(_MONEY), "f")
        payload["counterparty_country_code"] = _RESIDENCY[foreign]
        payload["counterparty_institution_reference"] = f"bank-{foreign.lower()}"
    return payload


def _channel(code: str, direction: str) -> tuple[str, str]:
    if code in {"regular_spend", "seasonal_spend", "unusual_amount", "new_counterparty", "merchant_sale"}:
        return "card", "purchase"
    if code == "high_frequency":
        return "card", "purchase"
    if code in {"salary", "recurring_payment"}:
        return "ach", "transfer"
    if code == "rapid_movement" and direction == "credit":
        return "wallet", "transfer"
    if code == "rapid_movement":
        return "ach", "transfer"
    return "wire", "transfer"


def _counterparty(
    spec: WorkloadSpec,
    party: PartyPlan,
    code: str,
    sequence: int,
    second_leg: bool = False,
    role: str = "",
) -> tuple[str, str, str | None]:
    if role == "card":
        merchant = sequence % len(_ORGANS)
        return (
            f"merchant-{spec.population_id}-{merchant:02d}",
            _ORGANS[merchant],
            None,
        )
    if role == "platform" or (code == "rapid_movement" and not second_leg and not role):
        return (f"platform-{spec.population_id}", "Bright Harbor Market", None)
    if (code in _PAIR_SHAPES or code in {"same_account_split", "same_account_partial"}) and not second_leg and role in {"", "receipt"}:
        if party.customer_type == "organization":
            return (
                f"buyer-{spec.population_id}-{party.index:04d}",
                "Northline Goods",
                None,
            )
        return (
            f"payer-{spec.population_id}-{party.index:04d}",
            "Bright Harbor Market",
            None,
        )
    if role == "utility":
        return (
            f"utility-{spec.population_id}-{party.index:04d}",
            "Lumen Freight",
            None,
        )
    if role == "payee":
        return (
            f"payee-{spec.population_id}-{party.index:04d}",
            "Sable Studio",
            None,
        )
    if role == "foreign" or (code == "cross_currency_outgoing" and second_leg):
        foreign = _CURRENCIES[(_CURRENCIES.index(party.currency_code) + 1) % len(_CURRENCIES)]
        return (
            f"foreign-{spec.population_id}-{party.index:04d}",
            "Oak Ledger Services",
            _RESIDENCY[foreign],
        )
    if role == "supplier" or code in {"same_account_split", "supplier_follow_on"}:
        name = _PAYEE_NAMES[sequence % len(_PAYEE_NAMES)]
        if code == "supplier_follow_on":
            name = "Quarry Tools"
        return (
            f"supplier-{spec.population_id}-{party.index:04d}-{sequence:06d}",
            name,
            None,
        )
    if code == "salary":
        return (
            f"employer-{spec.population_id}-{party.index:04d}",
            "Cedar Payroll",
            None,
        )
    if code == "recurring_payment":
        return (
            f"utility-{spec.population_id}-{party.index:04d}",
            "Lumen Freight",
            None,
        )
    if code == "new_counterparty":
        return (
            f"new-{spec.population_id}-{party.index:04d}-{sequence:06d}",
            "First Visit Merchant",
            None,
        )
    if code == "merchant_sale":
        buyer = sequence % 5
        return (
            f"buyer-{spec.population_id}-{party.index:04d}-{buyer}",
            _GIVEN[buyer] + " " + _FAMILY[buyer],
            None,
        )
    if code == "rapid_movement" and sequence % 2 == 0:
        return (f"platform-{spec.population_id}", "Bright Harbor Market", None)
    if code == "rapid_movement":
        return (f"supplier-{spec.population_id}-{party.index:04d}", "Fieldnote Supply", None)
    if code == "cross_border":
        return (
            f"foreign-{spec.population_id}-{party.index:04d}",
            "Oak Ledger Services",
            None,
        )
    merchant = sequence % len(_ORGANS)
    return (
        f"merchant-{spec.population_id}-{merchant:02d}",
        _ORGANS[merchant],
        None,
    )


def _legal_name(rng: Random, customer_type: str) -> str:
    if customer_type == "organization":
        return rng.choice(_ORGANS)
    return f"{rng.choice(_GIVEN)} {rng.choice(_FAMILY)}"

"""Audit of the Phase 10A payment dossiers.

Those dossiers are not loaded into the benchmark database. This module
measures whether their listed payments would meet the published R001 version 1
or R002 version 1 thresholds. It does not create alerts and it does not
assign investigation outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_PROFILES = ("consumer", "salaried", "merchant", "small_business")
_FAMILIES = (
    "merchant_settlement",
    "established_supplier",
    "unexplained_transfer",
    "new_counterparty",
)
_POPULATIONS = ("benchmark-fit", "benchmark-holdout")
_VARIANTS = ("v0", "v1", "v2", "v3", "e1", "e2")
_R001_WINDOW_HOURS = Decimal(1)
_R001_MINIMUM_DEBITS = 6
_R002_MINIMUM_INCOMING = Decimal(500)
_R002_RATIO = Decimal("0.80")
_R002_WINDOW_HOURS = Decimal(24)


@dataclass(frozen=True)
class Phase10APayment:
    """One Phase 10A listed payment, reduced to the fields a rule can see."""

    direction: str
    amount: Decimal
    hours_from_start: Decimal


@dataclass(frozen=True)
class Phase10AAudit:
    """How many Phase 10A dossiers meet a published rule threshold."""

    cases: int
    r001_threshold_met: int
    r002_threshold_met: int


def audit_phase10a() -> Phase10AAudit:
    """Apply the published thresholds to every Phase 10A dossier."""

    cases = _cases()
    return Phase10AAudit(
        cases=len(cases),
        r001_threshold_met=sum(1 for payments in cases if _meets_r001(payments)),
        r002_threshold_met=sum(1 for payments in cases if _meets_r002(payments)),
    )


def _cases() -> tuple[tuple[Phase10APayment, ...], ...]:
    rows = []
    for _population in _POPULATIONS:
        for profile in _PROFILES:
            for family in _FAMILIES:
                for variant in _VARIANTS:
                    rows.append(_payments(profile, family, variant))
    return tuple(rows)


def _payments(profile: str, family: str, variant: str) -> tuple[Phase10APayment, ...]:
    earlier_1, earlier_2, focal, follow = _amounts(profile, family)
    rows = [
        Phase10APayment("credit", earlier_1, Decimal(0)),
        Phase10APayment("credit", earlier_2, Decimal(24)),
        Phase10APayment("debit", focal, Decimal(48)),
    ]
    if variant == "v1":
        rows.append(Phase10APayment("debit", follow, Decimal(72)))
    if variant == "v3" or (variant == "e2" and profile in {"consumer", "merchant"}):
        rows.append(Phase10APayment("debit", follow, Decimal(72)))
    return tuple(rows)


def _amounts(profile: str, family: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    profile_step = Decimal(_PROFILES.index(profile) * 25)
    family_step = Decimal(_FAMILIES.index(family) * 10)
    earlier_1 = Decimal(100) + profile_step
    earlier_2 = earlier_1 + Decimal(40)
    focal = earlier_1 + earlier_2 + Decimal(80) + family_step
    follow = Decimal(55) + profile_step
    return earlier_1, earlier_2, focal, follow


def _meets_r001(payments: tuple[Phase10APayment, ...]) -> bool:
    debits = sorted(
        payment.hours_from_start for payment in payments if payment.direction == "debit"
    )
    for start in debits:
        inside = [hour for hour in debits if start <= hour <= start + _R001_WINDOW_HOURS]
        if len(inside) >= _R001_MINIMUM_DEBITS:
            return True
    return False


def _meets_r002(payments: tuple[Phase10APayment, ...]) -> bool:
    ordered = sorted(payments, key=lambda payment: payment.hours_from_start)
    open_receipts: list[list[Decimal]] = []
    for payment in ordered:
        if payment.direction == "credit" and payment.amount >= _R002_MINIMUM_INCOMING:
            open_receipts.append([payment.hours_from_start, payment.amount, Decimal(0)])
            continue
        if payment.direction != "debit":
            continue
        remaining = payment.amount
        for receipt in open_receipts:
            if remaining <= 0:
                break
            age = payment.hours_from_start - receipt[0]
            if age < 0 or age > _R002_WINDOW_HOURS or receipt[1] <= 0:
                continue
            allocated = min(remaining, receipt[1])
            receipt[1] -= allocated
            receipt[2] += allocated
            remaining -= allocated
            incoming = receipt[2] + receipt[1]
            if incoming > 0 and receipt[2] / incoming >= _R002_RATIO:
                return True
    return False

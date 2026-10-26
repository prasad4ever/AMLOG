"""Alert histories for the phase 10b benchmark.

Each history is a set of payments plus a support finding. The finding is the
only input to the judgment. The payments are loaded through ingestion so the
existing R001 and R002 replay can accept or reject them. A history that the
replay does not alert is not given an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from amlog.benchmark.protocol import SupportFinding

SOURCE_SYSTEM = "amlog-benchmark"
_ANCHOR = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PaymentSpec:
    """One payment to ingest for a benchmark account."""

    event_id: str
    direction: str
    amount: Decimal
    channel: str
    transaction_type: str
    counterparty: str
    minute_offset: int
    role: str


@dataclass(frozen=True)
class CaseSpec:
    """One constructed case before the monitoring replay has seen it."""

    case_id: str
    population_id: str
    population_role: str
    context_family: str
    customer_type: str
    currency_code: str
    anchor: datetime
    arrangement_on_file: bool
    arrangement_beneficiary: str | None
    payments: tuple[PaymentSpec, ...]
    subject_event_id: str
    expected_rule: str
    finding: SupportFinding

    @property
    def account_reference(self) -> str:
        return f"BM10B-{self.case_id}"

    @property
    def customer_reference(self) -> str:
        return f"BM10B-{self.case_id}"

    @property
    def opened_at(self) -> datetime:
        return self.anchor - timedelta(days=45)


def build_cases() -> tuple[CaseSpec, ...]:
    """Return the fit and holdout histories in a fixed order."""

    return (
        _merchant_amount("fit-ms-amount", "benchmark-fit", "fit", 0),
        _merchant_match("fit-ms-match", "benchmark-fit", "fit", 1),
        _supplier_payee("fit-es-payee", "benchmark-fit", "fit", 2),
        _supplier_match("fit-es-match", "benchmark-fit", "fit", 3),
        _transfer_absent("fit-ut-absent", "benchmark-fit", "fit", 4),
        _transfer_match("fit-ut-match", "benchmark-fit", "fit", 5),
        _counterparty_invoice("fit-nc-invoice", "benchmark-fit", "fit", 6),
        _counterparty_absent("fit-nc-absent", "benchmark-fit", "fit", 7),
        _holdout_merchant_payee("hold-ms-payee", "benchmark-holdout", "holdout", 8),
        _holdout_merchant_match("hold-ms-match", "benchmark-holdout", "holdout", 9),
        _holdout_supplier_amount("hold-es-amount", "benchmark-holdout", "holdout", 10),
        _holdout_supplier_match("hold-es-match", "benchmark-holdout", "holdout", 11),
        _holdout_transfer_payee("hold-ut-payee", "benchmark-holdout", "holdout", 12),
        _holdout_transfer_match("hold-ut-match", "benchmark-holdout", "holdout", 13),
        _holdout_counterparty_amount("hold-nc-amount", "benchmark-holdout", "holdout", 14),
        _holdout_counterparty_match("hold-nc-match", "benchmark-holdout", "holdout", 15),
    )


def _merchant_amount(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-settlement"
    return _case(
        case_id, population_id, role, index,
        family="merchant_settlement",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "120.00", "ach", f"cp-{case_id}-sales", -1440),
            *_burst(case_id, paid, "card", (0, 9, 17, 28, 36, 49), (
                "42.10", "18.40", "63.25", "27.80", "51.05", "33.60",
            ), start=2),
            _after(case_id, 8, "15.20", "card", paid, 49),
        ),
        subject_seq=7,
        finding=_mismatch(
            "settlement_report",
            beneficiary=True,
            amount=False,
            reference=True,
            note=(
                "The settlement report obtained in the review names the arranged "
                "settlement beneficiary and the payment reference, and it states a different amount."
            ),
        ),
    )


def _merchant_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-settlement"
    return _case(
        case_id, population_id, role, index,
        family="merchant_settlement",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "95.00", "ach", f"cp-{case_id}-sales", -1500),
            _debit(case_id, 2, "12.40", "ach", f"cp-{case_id}-fee", -180, "context"),
            *_burst(case_id, paid, "card", (2, 11, 19, 30, 41, 55), (
                "22.15", "44.70", "19.90", "58.30", "31.45", "27.05",
            ), start=3),
            _after(case_id, 9, "16.80", "card", paid, 55),
        ),
        subject_seq=8,
        finding=_match(
            "settlement_report",
            note=(
                "The settlement report obtained in the review names the same beneficiary, "
                "amount, and reference as the alerted card payout."
            ),
        ),
    )


def _supplier_payee(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-supplier"
    return _case(
        case_id, population_id, role, index,
        family="established_supplier",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "800.00", "ach", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "760.00", "wire", paid, 390, "subject"),
            _after(case_id, 3, "25.00", "wire", paid, 390),
        ),
        subject_seq=2,
        finding=_mismatch(
            "invoice",
            beneficiary=False,
            amount=True,
            reference=True,
            note=(
                "The invoice obtained in the review states the alerted amount and reference, "
                "and it names a different payee from the alerted payment."
            ),
        ),
    )


def _supplier_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-supplier"
    return _case(
        case_id, population_id, role, index,
        family="established_supplier",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "640.00", "ach", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "520.00", "ach", paid, 260, "subject"),
            _after(case_id, 3, "18.50", "ach", paid, 260),
        ),
        subject_seq=2,
        finding=_match(
            "invoice",
            note=(
                "The invoice obtained in the review names the same supplier, amount, "
                "and reference as the alerted payment."
            ),
        ),
    )


def _transfer_absent(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-beneficiary"
    return _case(
        case_id, population_id, role, index,
        family="unexplained_transfer",
        customer_type="individual",
        currency="EUR",
        arrangement=None,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "910.00", "wire", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "880.00", "wire", paid, 420, "subject"),
            _after(case_id, 3, "30.00", "wire", paid, 420),
        ),
        subject_seq=2,
        finding=_absent(
            "The review requested a support document for the alerted transfer. "
            "The file records that none was produced."
        ),
    )


def _transfer_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-beneficiary"
    return _case(
        case_id, population_id, role, index,
        family="unexplained_transfer",
        customer_type="individual",
        currency="EUR",
        arrangement=None,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "1200.00", "wire", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "400.00", "wire", paid, 200, "context"),
            _debit(case_id, 3, "600.00", "ach", paid, 700, "subject"),
            _after(case_id, 4, "22.00", "wire", paid, 700),
        ),
        subject_seq=3,
        finding=_match(
            "contract",
            note=(
                "The contract obtained in the review names the same beneficiary, amount, "
                "and reference as the alerted payment."
            ),
        ),
    )


def _counterparty_invoice(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-new"
    return _case(
        case_id, population_id, role, index,
        family="new_counterparty",
        customer_type="organization",
        currency="USD",
        arrangement=f"cp-{case_id}-arranged",
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "150.00", "ach", f"cp-{case_id}-sales", -1600),
            *_burst(case_id, paid, "card", (0, 8, 16, 27, 40, 51), (
                "37.25", "29.40", "48.10", "21.75", "55.60", "33.15",
            ), start=2, channels=("card", "card", "wallet", "card", "wallet", "card")),
            _after(case_id, 8, "11.10", "card", paid, 51),
        ),
        subject_seq=7,
        finding=_match(
            "invoice",
            note=(
                "The invoice obtained in the review names the same new beneficiary, amount, "
                "and reference as the alerted payment."
            ),
        ),
    )


def _counterparty_absent(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-first"
    return _case(
        case_id, population_id, role, index,
        family="new_counterparty",
        customer_type="individual",
        currency="USD",
        arrangement=None,
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "80.00", "ach", f"cp-{case_id}-sales", -1700),
            *_burst(case_id, paid, "card", (1, 10, 18, 29, 39, 54), (
                "26.80", "41.20", "17.65", "39.90", "52.30", "24.45",
            ), start=2, channels=("card", "card", "card", "card", "card", "wallet")),
            _after(case_id, 8, "13.70", "ach", paid, 54),
        ),
        subject_seq=7,
        finding=_absent(
            "The review requested support for the alerted payment to a beneficiary "
            "with no arrangement on the pre-alert file. The file records that none was produced."
        ),
    )


def _holdout_merchant_payee(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-settlement"
    minutes = (0, 5, 12, 18, 27, 38, 52)
    amounts = ("19.35", "28.60", "36.15", "47.80", "22.90", "31.25", "18.70")
    return _case(
        case_id, population_id, role, index,
        family="merchant_settlement",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R001",
        payments=(
            *_burst(case_id, paid, "card", minutes, amounts),
            _after(case_id, 8, "14.40", "wallet", paid, 38),
        ),
        subject_seq=6,
        finding=_mismatch(
            "settlement_report",
            beneficiary=False,
            amount=True,
            reference=True,
            note=(
                "The settlement report obtained in the review states the alerted amount "
                "and reference, and it names a different beneficiary."
            ),
        ),
    )


def _holdout_merchant_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-settlement"
    return _case(
        case_id, population_id, role, index,
        family="merchant_settlement",
        customer_type="organization",
        currency="USD",
        arrangement=paid,
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "110.00", "wire", f"cp-{case_id}-sales", -2000),
            *_burst(
                case_id, paid, "card", (3, 14, 24, 35, 46, 58),
                ("24.50", "33.80", "43.20", "18.90", "29.60", "37.40"),
                start=2,
                channels=("card", "card", "card", "card", "wallet", "wallet"),
            ),
            _after(case_id, 8, "12.25", "ach", paid, 58),
        ),
        subject_seq=7,
        finding=_match(
            "settlement_report",
            note=(
                "The settlement report obtained in the review matches the alerted payout "
                "on beneficiary, amount, and reference."
            ),
        ),
    )


def _holdout_supplier_amount(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-supplier"
    return _case(
        case_id, population_id, role, index,
        family="established_supplier",
        customer_type="organization",
        currency="EUR",
        arrangement=paid,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "1500.00", "ach", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "700.00", "ach", paid, 180, "context"),
            _debit(case_id, 3, "620.00", "wire", paid, 540, "subject"),
            _after(case_id, 4, "21.00", "ach", paid, 540),
        ),
        subject_seq=3,
        finding=_mismatch(
            "invoice",
            beneficiary=True,
            amount=False,
            reference=True,
            note=(
                "The invoice obtained in the review names the arranged supplier and the "
                "payment reference, and it states a different amount."
            ),
        ),
    )


def _holdout_supplier_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-supplier"
    return _case(
        case_id, population_id, role, index,
        family="established_supplier",
        customer_type="individual",
        currency="EUR",
        arrangement=paid,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "2000.00", "wire", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "1750.00", "wire", paid, 610, "subject"),
            _after(case_id, 3, "19.75", "wallet", paid, 610),
        ),
        subject_seq=2,
        finding=_match(
            "invoice",
            note=(
                "The invoice obtained in the review matches the alerted supplier payment "
                "on beneficiary, amount, and reference."
            ),
        ),
    )


def _holdout_transfer_payee(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-beneficiary"
    return _case(
        case_id, population_id, role, index,
        family="unexplained_transfer",
        customer_type="individual",
        currency="USD",
        arrangement=f"cp-{case_id}-arranged",
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "730.00", "ach", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "300.00", "ach", paid, 240, "context"),
            _debit(case_id, 3, "310.00", "ach", paid, 480, "subject"),
            _after(case_id, 4, "17.40", "wire", paid, 480),
        ),
        subject_seq=3,
        finding=_mismatch(
            "contract",
            beneficiary=False,
            amount=True,
            reference=True,
            note=(
                "The contract obtained in the review states the alerted amount and reference, "
                "and it names a different beneficiary from the alerted transfer."
            ),
        ),
    )


def _holdout_transfer_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-beneficiary"
    return _case(
        case_id, population_id, role, index,
        family="unexplained_transfer",
        customer_type="organization",
        currency="USD",
        arrangement=None,
        expected_rule="R002",
        payments=(
            _credit(case_id, 1, "560.00", "wallet", f"cp-{case_id}-receipt", 0),
            _debit(case_id, 2, "500.00", "wallet", paid, 90, "subject"),
            _after(case_id, 3, "16.10", "ach", paid, 90),
        ),
        subject_seq=2,
        finding=_match(
            "invoice",
            note=(
                "The invoice obtained in the review matches the alerted transfer "
                "on beneficiary, amount, and reference."
            ),
        ),
    )


def _holdout_counterparty_amount(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-new"
    return _case(
        case_id, population_id, role, index,
        family="new_counterparty",
        customer_type="individual",
        currency="USD",
        arrangement=None,
        expected_rule="R001",
        payments=(
            _credit(case_id, 1, "130.00", "wire", f"cp-{case_id}-sales", -1900),
            *_burst(case_id, paid, "card", (4, 13, 21, 32, 44, 57), (
                "61.40", "23.85", "44.10", "19.20", "38.75", "27.55",
            ), start=2),
            _after(case_id, 8, "15.90", "ach", paid, 57),
        ),
        subject_seq=7,
        finding=_mismatch(
            "invoice",
            beneficiary=True,
            amount=False,
            reference=True,
            note=(
                "The invoice obtained in the review names the alerted beneficiary and reference, "
                "and it states a different amount."
            ),
        ),
    )


def _holdout_counterparty_match(case_id: str, population_id: str, role: str, index: int) -> CaseSpec:
    paid = f"cp-{case_id}-new"
    return _case(
        case_id, population_id, role, index,
        family="new_counterparty",
        customer_type="organization",
        currency="USD",
        arrangement=f"cp-{case_id}-arranged",
        expected_rule="R001",
        payments=(
            *_burst(
                case_id, paid, "card", (6, 15, 23, 34, 45, 56),
                ("34.60", "28.15", "46.90", "21.30", "39.80", "25.45"),
                channels=("wallet", "card", "card", "wallet", "card", "card"),
            ),
            _after(case_id, 7, "12.80", "ach", paid, 56),
        ),
        subject_seq=6,
        finding=_match(
            "contract",
            note=(
                "The contract obtained in the review matches the alerted payment "
                "on beneficiary, amount, and reference."
            ),
        ),
    )


def _case(
    case_id: str,
    population_id: str,
    role: str,
    index: int,
    *,
    family: str,
    customer_type: str,
    currency: str,
    arrangement: str | None,
    expected_rule: str,
    payments: tuple[PaymentSpec, ...],
    subject_seq: int,
    finding: SupportFinding,
) -> CaseSpec:
    subject = f"bm10b:{case_id}:{subject_seq:02d}"
    event_ids = [payment.event_id for payment in payments]
    if len(event_ids) != len(set(event_ids)):
        raise RuntimeError(f"{case_id} repeats a source event id.")
    if not any(payment.event_id == subject and payment.role == "subject" for payment in payments):
        raise RuntimeError(f"{case_id} subject payment {subject} is missing.")
    if not any(payment.role == "after_alert" for payment in payments):
        raise RuntimeError(f"{case_id} has no payment after the alert.")
    return CaseSpec(
        case_id=case_id,
        population_id=population_id,
        population_role=role,
        context_family=family,
        customer_type=customer_type,
        currency_code=currency,
        anchor=_ANCHOR + timedelta(days=index * 3),
        arrangement_on_file=arrangement is not None,
        arrangement_beneficiary=arrangement,
        payments=payments,
        subject_event_id=subject,
        expected_rule=expected_rule,
        finding=finding,
    )


def _burst(
    case_id: str,
    counterparty: str,
    channel: str,
    minutes: tuple[int, ...],
    amounts: tuple[str, ...],
    *,
    start: int = 1,
    channels: tuple[str, ...] | None = None,
) -> tuple[PaymentSpec, ...]:
    rows = []
    for offset, (minute, amount) in enumerate(zip(minutes, amounts, strict=True)):
        used = channels[offset] if channels is not None else channel
        role = "subject" if offset == 5 else "context"
        rows.append(_debit(case_id, start + offset, amount, used, counterparty, minute, role))
    return tuple(rows)


def _credit(case_id: str, seq: int, amount: str, channel: str, counterparty: str, minute: int) -> PaymentSpec:
    return _payment(case_id, seq, "credit", amount, channel, counterparty, minute, "context")


def _debit(
    case_id: str,
    seq: int,
    amount: str,
    channel: str,
    counterparty: str,
    minute: int,
    role: str,
) -> PaymentSpec:
    return _payment(case_id, seq, "debit", amount, channel, counterparty, minute, role)


def _after(case_id: str, seq: int, amount: str, channel: str, counterparty: str, subject_minute: int) -> PaymentSpec:
    return _payment(
        case_id, seq, "debit", amount, channel, counterparty, subject_minute + 2160, "after_alert",
    )


def _payment(
    case_id: str,
    seq: int,
    direction: str,
    amount: str,
    channel: str,
    counterparty: str,
    minute: int,
    role: str,
) -> PaymentSpec:
    return PaymentSpec(
        event_id=f"bm10b:{case_id}:{seq:02d}",
        direction=direction,
        amount=Decimal(amount),
        channel=channel,
        transaction_type="purchase" if channel == "card" else "transfer",
        counterparty=counterparty,
        minute_offset=minute,
        role=role,
    )


def _match(kind: str, note: str) -> SupportFinding:
    return SupportFinding(kind, True, True, True, True, note)


def _mismatch(kind: str, *, beneficiary: bool, amount: bool, reference: bool, note: str) -> SupportFinding:
    return SupportFinding(kind, True, beneficiary, amount, reference, note)


def _absent(note: str) -> SupportFinding:
    return SupportFinding("not_produced", True, None, None, None, note)

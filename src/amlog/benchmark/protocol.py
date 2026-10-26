"""Benchmark judgment protocol for a linked AML alert.

The judgment reads one support finding obtained after the alert. It does not
read payment novelty, an amount deviation, earlier payments, a rule code, an
anomaly score, a scenario code, or a case disposition.

C1, productive review: the review obtained no support document, or the
document that was obtained does not match the alerted payment on beneficiary,
amount, or reference.

C2, non-productive review: the review obtained a support document that matches
the alerted payment on beneficiary, amount, and reference.

A productive judgment means the constructed file leaves a discrepancy to
pursue. It is not a finding of financial crime. A non-productive judgment
means the constructed file accounts for the alerted payment. It is not a
finding that no crime occurred.
"""

from __future__ import annotations

from dataclasses import dataclass


PRODUCTIVE = "productive_review"
NON_PRODUCTIVE = "non_productive_review"
ASSUMPTION_PRODUCTIVE = "C1"
ASSUMPTION_NON_PRODUCTIVE = "C2"
PROTOCOL_ACTOR = "amlog.benchmark.protocol"
JUDGMENT_SOURCE = "benchmark_protocol"
DESIGN_ID = "phase10b-alert-v1"
DOCUMENT_KINDS = ("invoice", "settlement_report", "contract", "not_produced")
DISCLAIMER = (
    "This is a benchmark judgment under the AMLOG case protocol. "
    "It is not an investigator finding, not a verified financial crime, "
    "and not an operational training label."
)


class ProtocolError(ValueError):
    """The support finding does not meet an assumption's evidence condition."""


@dataclass(frozen=True)
class SupportFinding:
    """What the constructed review file records after the alert."""

    document_kind: str
    obtained_after_alert: bool
    beneficiary_matches: bool | None
    amount_matches: bool | None
    reference_matches: bool | None
    note: str


@dataclass(frozen=True)
class ProtocolJudgment:
    """One protocol result and the evidence sentence that supports it."""

    outcome: str
    assumption_id: str
    rationale: str


def judge(finding: SupportFinding) -> ProtocolJudgment:
    """Apply C1 or C2 to one post-alert support finding."""

    if finding.obtained_after_alert is not True:
        raise ProtocolError(
            "The support finding is not marked as information obtained after the alert."
        )
    if not finding.note.strip():
        raise ProtocolError("The support finding has no evidence note.")
    comparisons = (
        finding.beneficiary_matches,
        finding.amount_matches,
        finding.reference_matches,
    )
    if finding.document_kind == "not_produced":
        if any(value is not None for value in comparisons):
            raise ProtocolError("A missing support document has no match flags.")
        return ProtocolJudgment(
            PRODUCTIVE,
            ASSUMPTION_PRODUCTIVE,
            (
                f"{finding.note} "
                "The review file records that the requested support document was not produced. "
                f"Assumption {ASSUMPTION_PRODUCTIVE}. {DISCLAIMER}"
            ),
        )
    if finding.document_kind not in DOCUMENT_KINDS:
        raise ProtocolError("The support document kind is not one of the protocol values.")
    if any(value is None for value in comparisons):
        raise ProtocolError("A produced support document must record each comparison.")
    if all(comparisons):
        return ProtocolJudgment(
            NON_PRODUCTIVE,
            ASSUMPTION_NON_PRODUCTIVE,
            (
                f"{finding.note} "
                "The support document matches the alerted payment on beneficiary, amount, and reference. "
                f"Assumption {ASSUMPTION_NON_PRODUCTIVE}. {DISCLAIMER}"
            ),
        )
    mismatches = [
        name
        for name, value in (
            ("beneficiary", finding.beneficiary_matches),
            ("amount", finding.amount_matches),
            ("reference", finding.reference_matches),
        )
        if value is False
    ]
    listed = ", ".join(mismatches)
    return ProtocolJudgment(
        PRODUCTIVE,
        ASSUMPTION_PRODUCTIVE,
        (
            f"{finding.note} "
            f"The support document does not match the alerted payment on {listed}. "
            f"Assumption {ASSUMPTION_PRODUCTIVE}. {DISCLAIMER}"
        ),
    )

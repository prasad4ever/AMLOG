"""Decide whether investigation-efficiency metrics can be calculated.

A productive label means a review was useful. The stored history does not
contain that judgment. Rule triggers, anomaly ranks, scenario codes, and
case dispositions are not substitutes for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from amlog.prioritize.names import MIN_SUPERVISED_PER_CLASS, MIN_SUPERVISED_REVIEWERS

# Counts only. These statements do not read scenario notes, rule codes, or scores.
_COUNT_QUERY = """
SELECT
    (SELECT count(*) FROM amlog.alerts),
    (SELECT count(*) FROM amlog.investigation_cases),
    (SELECT count(*) FROM amlog.investigation_notes),
    (SELECT count(*) FROM amlog_label.alert_labels),
    (SELECT count(*) FROM amlog.model_versions),
    (SELECT count(*) FROM amlog.risk_scores)
"""

_ELIGIBLE_QUERY = """
SELECT DISTINCT ON (alert_id)
    alert_id,
    label_value,
    labeled_by
FROM amlog_label.alert_labels
WHERE eligible_for_evaluation
  AND label_source IN ('investigator_outcome', 'quality_review')
  AND label_value IN ('productive_alert', 'non_productive_alert')
ORDER BY alert_id, labeled_at DESC, alert_label_id DESC
"""


class EvaluationBlocked(RuntimeError):
    """The comparison was not run because the outcome evidence is missing."""

    def __init__(self, evaluation: OutcomeEvaluation) -> None:
        super().__init__(evaluation.message)
        self.evaluation = evaluation


@dataclass(frozen=True)
class OutcomeEvidence:
    """What the database actually holds. Counts are not labels."""

    alerts: int
    investigation_cases: int
    investigation_notes: int
    label_rows: int
    eligible_productive: int
    eligible_non_productive: int
    distinct_eligible_reviewers: int
    model_versions: int
    risk_scores: int


@dataclass(frozen=True)
class OutcomeEvaluation:
    """The result of refusing to manufacture an efficiency comparison."""

    evidence: OutcomeEvidence
    missing: tuple[str, ...]

    @property
    def metrics_computed(self) -> bool:
        return False

    @property
    def message(self) -> str:
        lines = [
            "BLOCKED. A controlled investigation-outcome evaluation was not run.",
            "Rule triggers, anomaly ranks, scenario codes, and case dispositions are not investigation outcomes.",
            "Precision at review capacity was not computed.",
            "Recall of productive cases was not computed.",
            "Missed productive cases were not counted.",
            (
                f"Alerts: {self.evidence.alerts}. "
                f"Investigation cases: {self.evidence.investigation_cases}. "
                f"Investigation notes: {self.evidence.investigation_notes}. "
                f"Label rows: {self.evidence.label_rows}. "
                f"Eligible productive outcomes: {self.evidence.eligible_productive}. "
                f"Eligible non-productive outcomes: {self.evidence.eligible_non_productive}. "
                f"Distinct eligible reviewers: {self.evidence.distinct_eligible_reviewers}."
            ),
            "Missing evidence:",
            *[f"- {item}" for item in self.missing],
        ]
        return "\n".join(lines)


def load_outcome_evidence(connection: Any) -> OutcomeEvidence:
    """Read counts. This does not insert a label, a score, or a case."""

    alerts, cases, notes, labels, models, scores = connection.execute(_COUNT_QUERY).fetchone()
    rows = connection.execute(_ELIGIBLE_QUERY).fetchall()
    productive = sum(1 for row in rows if row[1] == "productive_alert")
    non_productive = sum(1 for row in rows if row[1] == "non_productive_alert")
    reviewers = len({row[2] for row in rows})
    return OutcomeEvidence(
        alerts=int(alerts),
        investigation_cases=int(cases),
        investigation_notes=int(notes),
        label_rows=int(labels),
        eligible_productive=productive,
        eligible_non_productive=non_productive,
        distinct_eligible_reviewers=reviewers,
        model_versions=int(models),
        risk_scores=int(scores),
    )


def assess_outcome_evidence(evidence: OutcomeEvidence) -> OutcomeEvaluation:
    """List the evidence a capacity comparison would need, and stop."""

    missing = []
    if evidence.investigation_cases < 1:
        missing.append("No investigation case records a human review.")
    if evidence.investigation_notes < 1:
        missing.append("No investigation note records what a reviewer examined or concluded.")
    if evidence.eligible_productive < MIN_SUPERVISED_PER_CLASS:
        missing.append(
            "Eligible reviewed productive outcomes are "
            f"{evidence.eligible_productive}; the floor is {MIN_SUPERVISED_PER_CLASS}. "
            "A synthetic fixture is not an eligible reviewed outcome."
        )
    if evidence.eligible_non_productive < MIN_SUPERVISED_PER_CLASS:
        missing.append(
            "Eligible reviewed non-productive outcomes are "
            f"{evidence.eligible_non_productive}; the floor is {MIN_SUPERVISED_PER_CLASS}. "
            "A synthetic fixture is not an eligible reviewed outcome."
        )
    if evidence.distinct_eligible_reviewers < MIN_SUPERVISED_REVIEWERS:
        missing.append(
            "Distinct eligible reviewers are "
            f"{evidence.distinct_eligible_reviewers}; the floor is {MIN_SUPERVISED_REVIEWERS}."
        )
    if not missing:
        missing.append(
            "The count floor is met, and this command still does not calculate review-capacity metrics."
        )
    return OutcomeEvaluation(evidence=evidence, missing=tuple(missing))

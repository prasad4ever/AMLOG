"""Supervised training gate.

An anomaly rank is not a false-positive model. This module trains only when
enough eligible, reviewed productive and non-productive outcomes exist, and
it does not publish precision, recall, or ROC-AUC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from amlog.features.compute import FEATURE_SET_NAME, FEATURE_SET_VERSION
from amlog.prioritize.errors import SupervisedTrainingBlocked
from amlog.prioritize.names import (
    MIN_SUPERVISED_PER_CLASS,
    MIN_SUPERVISED_REVIEWERS,
    MODEL_VERSION,
    SUPERVISED_FAMILY,
    SUPERVISED_MODEL_NAME,
)

_LABEL_QUERY = """
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


@dataclass(frozen=True)
class SupervisedReadiness:
    """Whether the supervised entry point is allowed to fit."""

    eligible_productive: int
    eligible_non_productive: int
    distinct_reviewers: int
    reasons: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)

    @property
    def message(self) -> str:
        if not self.reasons:
            return (
                "Eligible reviewed outcomes meet the entry-point floor. "
                "No precision, recall, or ROC-AUC is produced by this check."
            )
        return (
            "BLOCKED. Supervised training and false-positive reduction evaluation were not run. "
            + " ".join(self.reasons)
        )


def assess_supervised_readiness(connection: Any) -> SupervisedReadiness:
    """Count the latest eligible reviewed outcome on each alert."""

    rows = connection.execute(_LABEL_QUERY).fetchall()
    productive = sum(1 for row in rows if row[1] == "productive_alert")
    non_productive = sum(1 for row in rows if row[1] == "non_productive_alert")
    reviewers = len({row[2] for row in rows})
    reasons = []
    if productive < MIN_SUPERVISED_PER_CLASS:
        reasons.append(
            f"Eligible productive outcomes are {productive}; the floor is {MIN_SUPERVISED_PER_CLASS}."
        )
    if non_productive < MIN_SUPERVISED_PER_CLASS:
        reasons.append(
            f"Eligible non-productive outcomes are {non_productive}; the floor is {MIN_SUPERVISED_PER_CLASS}."
        )
    if reviewers < MIN_SUPERVISED_REVIEWERS:
        reasons.append(
            f"Distinct reviewers are {reviewers}; the floor is {MIN_SUPERVISED_REVIEWERS}."
        )
    return SupervisedReadiness(
        eligible_productive=productive,
        eligible_non_productive=non_productive,
        distinct_reviewers=reviewers,
        reasons=tuple(reasons),
    )


def train_supervised(connection: Any) -> UUID:
    """Fit only when the gate is open. The version stays registered, not approved."""

    readiness = assess_supervised_readiness(connection)
    if readiness.blocked:
        raise SupervisedTrainingBlocked(readiness.message)
    return _fit_registered_model(connection, readiness)


def _fit_registered_model(connection: Any, readiness: SupervisedReadiness) -> UUID:
    from psycopg.types.json import Json
    from sklearn.linear_model import LogisticRegression

    from amlog.prioritize.preprocess import prepare
    from amlog.prioritize.store import _guard, _load_samples

    _guard(connection)
    labels = {
        row[0]: 1 if row[1] == "productive_alert" else 0
        for row in connection.execute(_LABEL_QUERY).fetchall()
    }
    samples = tuple(sample for sample in _load_samples(connection) if sample.alert_id in labels)
    prepared = prepare(samples, fraction=1.0)
    model = LogisticRegression(max_iter=500, random_state=20261026)
    model.fit(prepared.rows, [labels[sample.alert_id] for sample in samples])
    del model
    summary = {
        "metrics_computed": False,
        "reason": "This entry point fits a registered model and does not report a held-out evaluation.",
        "productive_alerts": readiness.eligible_productive,
        "non_productive_alerts": readiness.eligible_non_productive,
        "distinct_reviewers": readiness.distinct_reviewers,
        "production_approved": False,
        "lifecycle_status": "registered",
    }
    provenance = (
        "Supervised logistic regression using the latest eligible investigator or quality-review "
        "outcome on each alert. Labels came from the label schema, not from scenario codes or rule codes. "
        "Precision, recall, and ROC-AUC were not computed. This version is not validated and not approved. "
        "The underlying alerts are development data unless a later provenance statement says otherwise."
    )
    with connection.transaction():
        model_id = connection.execute(
            """
            INSERT INTO amlog.model_versions (
                model_name, version_label, algorithm_family, feature_set_name, feature_set_version,
                training_data_provenance, evaluation_summary, lifecycle_status, registered_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'registered', 'amlog.prioritize')
            RETURNING model_version_id
            """,
            (
                SUPERVISED_MODEL_NAME,
                MODEL_VERSION,
                SUPERVISED_FAMILY,
                FEATURE_SET_NAME,
                FEATURE_SET_VERSION,
                provenance,
                Json(summary),
            ),
        ).fetchone()[0]
        connection.execute(
            """
            SELECT amlog.record_audit_event(
                'batch'::text, 'amlog.prioritize'::text, 'supervised_model_registered'::text,
                'amlog'::text, 'model_versions'::text, %s::uuid, NULL::uuid, NULL::text,
                NULL::jsonb, %s::jsonb, %s::jsonb
            )
            """,
            (
                model_id,
                Json({"lifecycle_status": "registered", "metrics_computed": False}),
                Json({"production_approved": False}),
            ),
        )
    connection.commit()
    return model_id

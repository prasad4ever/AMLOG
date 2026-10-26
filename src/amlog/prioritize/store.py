"""Load snapshots, fit the two experiment rankings, and store the scores.

The models are moved to ``validated`` so the database will accept experiment
scores. They are not approved, and operational scores are not written.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any
from uuid import UUID, uuid4

import sklearn

from amlog.features.compute import FEATURE_SET_NAME, FEATURE_SET_VERSION
from amlog.prioritize.artifact import (
    artifact_file,
    default_artifact_dir,
    load_ranking_artifact,
    parameter_reference,
    save_ranking_artifacts,
    score_saved,
)
from amlog.prioritize.errors import PriorityError
from amlog.prioritize.names import (
    BASELINE_FAMILY,
    BASELINE_MODEL_NAME,
    BASELINE_SEMANTICS,
    ISOLATION_FAMILY,
    ISOLATION_MODEL_NAME,
    ISOLATION_SEMANTICS,
    MODEL_VERSION,
    N_ESTIMATORS,
    RANDOM_SEED,
    TRAINING_FRACTION,
)
from amlog.prioritize.preprocess import AlertSample, prepare
from amlog.prioritize.rank import rank_alerts

PROTECTED_DATABASES = frozenset({"amlog", "postgres", "template0", "template1"})
_SCORE_QUANTUM = Decimal("0.00000001")
_ACTOR = "amlog.prioritize"

_COMPUTATION_QUERY = """
SELECT
    alerts.alert_id,
    alerts.account_id,
    feature_computations.feature_computation_id,
    feature_computations.input_fingerprint,
    feature_computations.feature_set_name,
    feature_computations.feature_set_version
FROM amlog.alerts AS alerts
JOIN amlog.feature_computations AS feature_computations
  ON feature_computations.feature_computation_id = (
      SELECT latest.feature_computation_id
      FROM amlog.feature_computations AS latest
      WHERE latest.alert_id = alerts.alert_id
        AND latest.feature_set_name = %s
        AND latest.feature_set_version = %s
      ORDER BY latest.computed_at DESC, latest.feature_computation_id
      LIMIT 1
  )
ORDER BY alerts.alert_id
"""

_FEATURE_QUERY = """
SELECT
    feature_computation_id,
    feature_name,
    value_type,
    value_numeric,
    value_boolean,
    value_text
FROM amlog.alert_features
WHERE feature_computation_id = ANY(%s)
"""


@dataclass(frozen=True)
class PriorityRun:
    """What one scoring command did."""

    alerts_seen: int
    scores_written: int
    already_present: int
    training_alerts: int
    holdout_alerts: int
    holdout_rank_agreement: float | None
    elapsed_seconds: float


def assert_priority_database(database_name: str) -> None:
    """Refuse the development database and PostgreSQL's own databases."""

    if database_name in PROTECTED_DATABASES:
        raise PriorityError(
            f"{database_name} is not a scoring database. No model or score was written."
        )


def run_prioritization(
    connection: Any,
    *,
    n_estimators: int = N_ESTIMATORS,
    random_state: int = RANDOM_SEED,
    fraction: float = TRAINING_FRACTION,
) -> PriorityRun:
    """Score the latest alert_behavior snapshots once for version 1."""

    started = time.perf_counter()
    _guard(connection)
    samples = _load_samples(connection)
    if _version_is_complete(connection, samples):
        _ensure_saved_artifacts(
            connection,
            samples,
            n_estimators=n_estimators,
            random_state=random_state,
            fraction=fraction,
        )
        return PriorityRun(
            alerts_seen=len(samples),
            scores_written=0,
            already_present=len(samples) * 2,
            training_alerts=0,
            holdout_alerts=0,
            holdout_rank_agreement=None,
            elapsed_seconds=time.perf_counter() - started,
        )
    prepared = prepare(samples, fraction=fraction, seed=random_state)
    ranking = rank_alerts(prepared, n_estimators=n_estimators, random_state=random_state)
    scoring_run_id = uuid4()
    scored_at = datetime.now(timezone.utc)
    with connection.transaction():
        isolation_id = _register_model(
            connection,
            model_name=ISOLATION_MODEL_NAME,
            algorithm_family=ISOLATION_FAMILY,
            provenance=_provenance("Isolation Forest"),
            summary=_summary(prepared, ranking, n_estimators=n_estimators, random_state=random_state),
            artifact=parameter_reference(prepared, n_estimators=n_estimators, random_state=random_state),
        )
        baseline_id = _register_model(
            connection,
            model_name=BASELINE_MODEL_NAME,
            algorithm_family=BASELINE_FAMILY,
            provenance=_provenance("robust z-score average"),
            summary=_summary(prepared, ranking, n_estimators=None, random_state=random_state),
            artifact=parameter_reference(prepared, n_estimators=None, random_state=random_state),
        )
        written = _insert_scores(
            connection,
            samples,
            ranking,
            isolation_id,
            baseline_id,
            scoring_run_id,
            scored_at,
        )
        _audit(
            connection,
            "scores_recorded",
            isolation_id,
            None,
            {"scores_written": written, "scoring_run_id": str(scoring_run_id)},
            {"score_use": "experiment", "production_approved": False},
        )
    connection.commit()
    save_ranking_artifacts(
        prepared,
        ranking,
        isolation_reference=parameter_reference(prepared, n_estimators=n_estimators, random_state=random_state),
        baseline_reference=parameter_reference(prepared, n_estimators=None, random_state=random_state),
        n_estimators=n_estimators,
        random_state=random_state,
    )
    return PriorityRun(
        alerts_seen=len(samples),
        scores_written=written,
        already_present=0,
        training_alerts=ranking.training_alerts,
        holdout_alerts=ranking.holdout_alerts,
        holdout_rank_agreement=ranking.holdout_rank_agreement,
        elapsed_seconds=time.perf_counter() - started,
    )


def _guard(connection: Any) -> None:
    name = connection.execute("SELECT current_database()").fetchone()[0]
    assert_priority_database(str(name))


def _load_samples(connection: Any) -> tuple[AlertSample, ...]:
    headers = connection.execute(
        _COMPUTATION_QUERY, (FEATURE_SET_NAME, FEATURE_SET_VERSION)
    ).fetchall()
    if not headers:
        raise PriorityError("No alert_behavior snapshots are stored.")
    computation_ids = [row[2] for row in headers]
    feature_rows = connection.execute(_FEATURE_QUERY, (computation_ids,)).fetchall()
    by_computation: dict[UUID, dict[str, float | None]] = {}
    currencies: dict[UUID, str | None] = {}
    for computation_id, name, value_type, numeric, boolean, text in feature_rows:
        if name == "amount_currency" and value_type == "text":
            currencies[computation_id] = text.strip().upper() if text else None
            continue
        number = _as_float(value_type, numeric, boolean)
        if number is None and value_type != "numeric":
            continue
        by_computation.setdefault(computation_id, {})[name] = number
    samples = []
    for alert_id, account_id, computation_id, fingerprint, feature_set_name, feature_set_version in headers:
        if account_id is None:
            continue
        samples.append(
            AlertSample(
                alert_id=alert_id,
                account_id=account_id,
                currency=currencies.get(computation_id),
                values=by_computation.get(computation_id, {}),
                feature_computation_id=computation_id,
                fingerprint=fingerprint,
                feature_set_name=feature_set_name,
                feature_set_version=feature_set_version,
            )
        )
    if len(samples) < 2:
        raise PriorityError("Fewer than two alerts have an account and a feature snapshot.")
    return tuple(samples)


def _as_float(value_type: str, numeric: Any, boolean: Any) -> float | None:
    if value_type == "numeric" and numeric is not None:
        return float(numeric)
    if value_type == "boolean" and boolean is not None:
        return 1.0 if boolean else 0.0
    return None


def _version_is_complete(connection: Any, samples: tuple[AlertSample, ...]) -> bool:
    isolation = _model_row(connection, ISOLATION_MODEL_NAME)
    baseline = _model_row(connection, BASELINE_MODEL_NAME)
    if isolation is None and baseline is None:
        return False
    if isolation is None or baseline is None or isolation[1] != "validated" or baseline[1] != "validated":
        raise PriorityError(
            "Version 1 is already registered in a state that cannot accept these scores. "
            "A changed ranking needs a new version label."
        )
    stored = {
        (row[0], row[1], row[2])
        for row in connection.execute(
            """
            SELECT alert_id, model_version_id, recorded_input_fingerprint
            FROM amlog.risk_scores
            WHERE model_version_id IN (%s, %s)
            """,
            (isolation[0], baseline[0]),
        ).fetchall()
    }
    for sample in samples:
        if (sample.alert_id, isolation[0], sample.fingerprint) not in stored:
            raise PriorityError(
                "Version 1 does not cover every current snapshot. A new snapshot needs a new version label."
            )
        if (sample.alert_id, baseline[0], sample.fingerprint) not in stored:
            raise PriorityError(
                "Version 1 does not cover every current snapshot. A new snapshot needs a new version label."
            )
    return True


def _model_row(connection: Any, model_name: str) -> tuple[UUID, str] | None:
    row = connection.execute(
        """
        SELECT model_version_id, lifecycle_status
        FROM amlog.model_versions
        WHERE model_name = %s AND version_label = %s
        """,
        (model_name, MODEL_VERSION),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _register_model(
    connection: Any,
    *,
    model_name: str,
    algorithm_family: str,
    provenance: str,
    summary: dict[str, Any],
    artifact: str,
) -> UUID:
    from psycopg.types.json import Json

    model_id = connection.execute(
        """
        INSERT INTO amlog.model_versions (
            model_name, version_label, algorithm_family, feature_set_name, feature_set_version,
            training_data_provenance, artifact_reference, evaluation_summary,
            lifecycle_status, registered_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'registered', %s)
        RETURNING model_version_id
        """,
        (
            model_name,
            MODEL_VERSION,
            algorithm_family,
            FEATURE_SET_NAME,
            FEATURE_SET_VERSION,
            provenance,
            artifact,
            Json(summary),
            _ACTOR,
        ),
    ).fetchone()[0]
    _audit(
        connection,
        "model_registered",
        model_id,
        None,
        {"lifecycle_status": "registered", "model_name": model_name, "version_label": MODEL_VERSION},
        {"production_approved": False},
    )
    connection.execute(
        """
        UPDATE amlog.model_versions
        SET lifecycle_status = 'validated'
        WHERE model_version_id = %s
          AND lifecycle_status = 'registered'
        """,
        (model_id,),
    )
    _audit(
        connection,
        "model_validated_for_experiment",
        model_id,
        {"lifecycle_status": "registered"},
        {
            "lifecycle_status": "validated",
            "meaning": "Experiment scores are allowed. This is not production approval.",
        },
        {"production_approved": False},
    )
    return model_id


def _insert_scores(
    connection: Any,
    samples: tuple[AlertSample, ...],
    ranking: Any,
    isolation_id: UUID,
    baseline_id: UUID,
    scoring_run_id: UUID,
    scored_at: datetime,
) -> int:
    from psycopg.types.json import Json

    rows = []
    for sample, ranked in zip(samples, ranking.rows):
        shared = {
            "sample_role": ranked.sample_role,
            "alerts_scored": len(samples),
            "statement": ISOLATION_SEMANTICS,
            "score_use": "experiment",
            "model_lifecycle": "validated",
            "production_approved": False,
        }
        rows.append(
            (
                sample.alert_id,
                isolation_id,
                ISOLATION_MODEL_NAME,
                MODEL_VERSION,
                ISOLATION_FAMILY,
                sample.feature_set_name,
                sample.feature_set_version,
                sample.feature_computation_id,
                sample.fingerprint,
                _decimal(ranked.anomaly_score),
                ISOLATION_SEMANTICS,
                Json(
                    {
                        **shared,
                        "rank": ranked.anomaly_rank,
                        "baseline_rank": ranked.baseline_rank,
                        "baseline_score": f"{ranked.baseline_score:.8f}",
                        "top_features": [
                            {"name": name, "robust_z": value} for name, value in ranked.top_features
                        ],
                    }
                ),
                scoring_run_id,
                scored_at,
            )
        )
        rows.append(
            (
                sample.alert_id,
                baseline_id,
                BASELINE_MODEL_NAME,
                MODEL_VERSION,
                BASELINE_FAMILY,
                sample.feature_set_name,
                sample.feature_set_version,
                sample.feature_computation_id,
                sample.fingerprint,
                _decimal(ranked.baseline_score),
                BASELINE_SEMANTICS,
                Json(
                    {
                        **shared,
                        "statement": BASELINE_SEMANTICS,
                        "rank": ranked.baseline_rank,
                        "comparison_model": ISOLATION_MODEL_NAME,
                    }
                ),
                scoring_run_id,
                scored_at,
            )
        )
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO amlog.risk_scores (
                alert_id, model_version_id, recorded_model_name, recorded_version_label,
                recorded_algorithm_family, feature_set_name, feature_set_version,
                feature_computation_id, recorded_input_fingerprint, score, score_semantics,
                score_use, explanation_document, scoring_run_id, scored_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'experiment', %s, %s, %s
            )
            """,
            rows,
        )
    return len(rows)


def _summary(prepared: Any, ranking: Any, *, n_estimators: int | None, random_state: int) -> dict[str, Any]:
    agreement = ranking.holdout_rank_agreement
    return {
        "score_use": "experiment",
        "production_approved": False,
        "validated_for_experiment_scores": True,
        "validation_meaning": (
            "Validated means this version may write experiment scores. "
            "It is not approved for operational scoring and it is not a regulatory validation."
        ),
        "supervised_false_positive_evaluation": "blocked",
        "training_alerts": ranking.training_alerts,
        "holdout_alerts": ranking.holdout_alerts,
        "random_state": random_state,
        "n_estimators": n_estimators,
        "columns": list(prepared.columns),
        "dropped_constant_columns": list(prepared.dropped_constant),
        "currency_scaling": (
            "Amounts are divided by the training-account median absolute deviation "
            "inside the same currency. A currency with fewer than "
            f"{8} training observations, or a zero spread, is left missing."
        ),
        "holdout_rank_agreement_with_baseline": None if agreement is None else round(agreement, 6),
        "limitations": [
            "The alerts come from a generated history. Repeated account behavior and designed scenarios are correlated.",
            "Training-account scores are in-sample. Holdout scores use accounts that were not in the fit.",
            "No eligible reviewed outcome was used, so precision, recall, and ROC-AUC are not computed.",
            "A high rank is not evidence that a crime occurred.",
        ],
    }


def _ensure_saved_artifacts(
    connection: Any,
    samples: tuple[AlertSample, ...],
    *,
    n_estimators: int,
    random_state: int,
    fraction: float,
) -> None:
    """Reload a saved ranking, or write one when the file is missing.

    A missing file is rebuilt in memory and kept only when it matches the
    stored parameter digest and the stored scores. No score row is inserted
    and no validated model row is updated.
    """

    isolation_reference = _artifact_reference(connection, ISOLATION_MODEL_NAME)
    baseline_reference = _artifact_reference(connection, BASELINE_MODEL_NAME)
    if isolation_reference is None or baseline_reference is None:
        raise PriorityError("Version 1 has no parameter digest. No artifact file was written.")
    directory = default_artifact_dir()
    isolation_path = artifact_file(directory, isolation_reference)
    baseline_path = artifact_file(directory, baseline_reference)
    if isolation_path.is_file() and baseline_path.is_file():
        _assert_saved_scores_match(connection, samples, isolation_reference, baseline_reference)
        return
    prepared = prepare(samples, fraction=fraction, seed=random_state)
    ranking = rank_alerts(prepared, n_estimators=n_estimators, random_state=random_state)
    fitted_isolation = parameter_reference(prepared, n_estimators=n_estimators, random_state=random_state)
    fitted_baseline = parameter_reference(prepared, n_estimators=None, random_state=random_state)
    if fitted_isolation != isolation_reference or fitted_baseline != baseline_reference:
        raise PriorityError(
            "Version 1 is already stored, and these settings do not match its parameter digest. "
            "No score was written and no artifact file was replaced."
        )
    if not _ranking_matches_stored(connection, samples, ranking):
        raise PriorityError(
            "Version 1 is already stored, and a refit did not reproduce its scores. "
            "No score was written and no artifact file was written."
        )
    save_ranking_artifacts(
        prepared,
        ranking,
        isolation_reference=isolation_reference,
        baseline_reference=baseline_reference,
        n_estimators=n_estimators,
        random_state=random_state,
    )


def _artifact_reference(connection: Any, model_name: str) -> str | None:
    row = connection.execute(
        """
        SELECT artifact_reference
        FROM amlog.model_versions
        WHERE model_name = %s AND version_label = %s
        """,
        (model_name, MODEL_VERSION),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _assert_saved_scores_match(
    connection: Any,
    samples: tuple[AlertSample, ...],
    isolation_reference: str,
    baseline_reference: str,
) -> None:
    isolation_scores = score_saved(samples, load_ranking_artifact(isolation_reference))
    baseline_scores = score_saved(samples, load_ranking_artifact(baseline_reference))
    stored = _stored_scores(connection)
    for sample, anomaly, baseline in zip(samples, isolation_scores, baseline_scores):
        if stored.get((sample.alert_id, ISOLATION_MODEL_NAME)) != _decimal(anomaly):
            raise PriorityError(
                "The saved anomaly artifact did not reproduce the stored scores. The scores were left unchanged."
            )
        if stored.get((sample.alert_id, BASELINE_MODEL_NAME)) != _decimal(baseline):
            raise PriorityError(
                "The saved baseline artifact did not reproduce the stored scores. The scores were left unchanged."
            )


def _ranking_matches_stored(connection: Any, samples: tuple[AlertSample, ...], ranking: Any) -> bool:
    stored = _stored_scores(connection)
    for sample, ranked in zip(samples, ranking.rows):
        if stored.get((sample.alert_id, ISOLATION_MODEL_NAME)) != _decimal(ranked.anomaly_score):
            return False
        if stored.get((sample.alert_id, BASELINE_MODEL_NAME)) != _decimal(ranked.baseline_score):
            return False
    return True


def _stored_scores(connection: Any) -> dict[tuple[UUID, str], Decimal]:
    rows = connection.execute(
        """
        SELECT alert_id, recorded_model_name, score
        FROM amlog.risk_scores
        WHERE recorded_version_label = %s
          AND recorded_model_name IN (%s, %s)
        """,
        (MODEL_VERSION, ISOLATION_MODEL_NAME, BASELINE_MODEL_NAME),
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def _provenance(algorithm: str) -> str:
    return (
        f"Unsupervised {algorithm} fit on feature set {FEATURE_SET_NAME} version {FEATURE_SET_VERSION}. "
        "The rows are fictional development alerts already stored in the selected disposable database. "
        "No evaluation label, investigation disposition, or scenario code was an input. "
        "Accounts were split by a fixed hash so one account stays entirely in the fit or entirely in the holdout. "
        "Scores on fit accounts are in-sample. "
        "This is not operational training data and this version is not approved for production. "
        f"scikit-learn {sklearn.__version__}."
    )


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.8f}").quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _audit(
    connection: Any,
    action: str,
    entity_id: UUID,
    previous: dict | None,
    resulting: dict | None,
    context: dict | None,
) -> None:
    from psycopg.types.json import Json

    connection.execute(
        """
        SELECT amlog.record_audit_event(
            'batch'::text,
            %s::text,
            %s::text,
            'amlog'::text,
            'model_versions'::text,
            %s::uuid,
            NULL::uuid,
            NULL::text,
            %s::jsonb,
            %s::jsonb,
            %s::jsonb
        )
        """,
        (
            _ACTOR,
            action,
            entity_id,
            Json(previous) if previous is not None else None,
            Json(resulting) if resulting is not None else None,
            Json(context) if context is not None else None,
        ),
    )

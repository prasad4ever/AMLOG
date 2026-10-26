"""Run the declared SynthAML train, score, and equal-capacity comparison.

Outcomes are joined only after the feature matrix exists. The operational
label store and the operational supervised gate are not used.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from amlog.synthaml.dataset import file_sha256, load_alert_dates, load_outcomes
from amlog.synthaml.errors import SynthamlError, SynthamlLeakageError
from amlog.synthaml.features import build_feature_matrix
from amlog.synthaml.metrics import CapacityComparison, compare_capacities
from amlog.synthaml.model import fit_report_model, save_model_bundle, score_report_model
from amlog.synthaml.names import (
    ALGORITHM_FAMILY,
    CHRONOLOGY_LIMIT,
    ENTRY_DEFINITION,
    EXPERIMENT_ID,
    FEATURE_NAMES,
    FEATURE_SET_NAME,
    FEATURE_SET_VERSION,
    LIMITATIONS,
    LOGISTIC_C,
    MAX_ITER,
    MISSING_GROUP_RULE,
    MODEL_NAME,
    MODEL_VERSION,
    PAPER_DOI,
    PUBLISHED_ALERTS,
    PUBLISHED_DISMISSALS,
    PUBLISHED_REPORTS,
    PUBLISHED_TEST_ALERTS,
    PUBLISHED_TEST_REPORTS,
    PUBLISHED_TRAIN_ALERTS,
    PUBLISHED_TRAIN_REPORTS,
    PUBLISHED_TRANSACTION_ROWS,
    RANDOM_SEED,
    SCORE_SEMANTICS,
    SIZE_DEFINITION,
    SOLVER,
)
from amlog.synthaml.preprocess import apply_training_scale, fit_training_scale


@dataclass(frozen=True)
class PopulationCounts:
    """Identity counts for the published extract."""

    alerts: int
    train_alerts: int
    test_alerts: int
    reports: int
    dismissals: int
    train_reports: int
    test_reports: int
    transaction_rows: int
    future_rows: int
    older_than_365_rows: int
    alerts_without_transactions: int


@dataclass(frozen=True)
class ExperimentResult:
    """Held-out comparison and the provenance needed to read it."""

    experiment_id: str
    model_name: str
    model_version: str
    algorithm_family: str
    feature_set_name: str
    feature_set_version: str
    random_state: int
    sklearn_version: str
    solver: str
    logistic_c: float
    max_iter: int
    n_iter: int
    converged: bool
    intercept: float
    coefficient_count: int
    train_alerts: int
    test_alerts: int
    train_reports: int
    train_dismisses: int
    test_reports: int
    test_dismisses: int
    transaction_rows: int
    future_rows: int
    older_than_365_rows: int
    feature_seconds: float
    fit_seconds: float
    total_seconds: float
    alert_sha256: str
    transaction_sha256: str
    feature_matrix_sha256: str
    grouped_read: bool
    comparisons: tuple[CapacityComparison, ...]
    model_path: str
    evaluation_path: str
    limitations: tuple[str, ...]


def assert_published_population(counts: PopulationCounts) -> None:
    """Stop when the files are not the published 20,000-alert extract."""

    expected = {
        "alerts": (counts.alerts, PUBLISHED_ALERTS),
        "train_alerts": (counts.train_alerts, PUBLISHED_TRAIN_ALERTS),
        "test_alerts": (counts.test_alerts, PUBLISHED_TEST_ALERTS),
        "reports": (counts.reports, PUBLISHED_REPORTS),
        "dismissals": (counts.dismissals, PUBLISHED_DISMISSALS),
        "train_reports": (counts.train_reports, PUBLISHED_TRAIN_REPORTS),
        "test_reports": (counts.test_reports, PUBLISHED_TEST_REPORTS),
        "transaction_rows": (counts.transaction_rows, PUBLISHED_TRANSACTION_ROWS),
        "future_rows": (counts.future_rows, 0),
        "older_than_365_rows": (counts.older_than_365_rows, 0),
        "alerts_without_transactions": (counts.alerts_without_transactions, 0),
    }
    mismatches = [
        f"{name} is {actual}"
        for name, (actual, target) in expected.items()
        if actual != target
    ]
    if mismatches:
        raise SynthamlError(
            "The files do not match the published SynthAML population. " + " ".join(mismatches)
        )


def run_experiment(
    alerts_path: Path,
    transactions_path: Path,
    artifact_dir: Path,
    *,
    published_checks: bool = False,
    progress: Callable[[int], None] | None = None,
) -> ExperimentResult:
    """Fit on 2020 and compare review orders on 2021. One model, no capacity search."""

    started = time.perf_counter()
    alert_dates = load_alert_dates(alerts_path)
    outcomes = load_outcomes(alerts_path)
    if set(alert_dates) != set(outcomes):
        raise SynthamlError("Alert dates and outcomes do not describe the same alerts.")
    feature_started = time.perf_counter()
    built = build_feature_matrix(alert_dates, transactions_path, progress)
    feature_seconds = time.perf_counter() - feature_started
    if built.future_rows:
        raise SynthamlLeakageError(
            f"{built.future_rows} transactions are dated after their alert's published date. "
            "They were excluded from the feature matrix, and the model was not fit."
        )
    if built.matrix.shape != (len(built.alert_ids), len(FEATURE_NAMES)):
        raise SynthamlError("The feature matrix does not have the published 56 columns.")
    train_mask, test_mask = _year_masks(built.alert_dates)
    y = np.asarray(
        [1 if outcomes[alert_id] == "Report" else 0 for alert_id in built.alert_ids],
        dtype=np.int64,
    )
    counts = PopulationCounts(
        alerts=len(built.alert_ids),
        train_alerts=int(train_mask.sum()),
        test_alerts=int(test_mask.sum()),
        reports=int(y.sum()),
        dismissals=int(len(y) - y.sum()),
        train_reports=int(y[train_mask].sum()),
        test_reports=int(y[test_mask].sum()),
        transaction_rows=built.transaction_rows,
        future_rows=built.future_rows,
        older_than_365_rows=built.older_than_365_rows,
        alerts_without_transactions=built.alerts_without_transactions,
    )
    if published_checks:
        assert_published_population(counts)
    scale = fit_training_scale(built.matrix[train_mask])
    scaled = apply_training_scale(built.matrix, scale)
    fit_started = time.perf_counter()
    estimator, fitted = fit_report_model(scaled[train_mask], y[train_mask])
    test_scores = score_report_model(estimator, scaled[test_mask])
    fit_seconds = time.perf_counter() - fit_started
    test_ids = tuple(alert_id for alert_id, keep in zip(built.alert_ids, test_mask, strict=True) if keep)
    test_dates = tuple(alert_day for alert_day, keep in zip(built.alert_dates, test_mask, strict=True) if keep)
    comparisons = compare_capacities(
        test_ids,
        test_dates,
        tuple(float(score) for score in test_scores),
        outcomes,
        counts.test_reports,
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / "synthaml_report_logistic-1.joblib"
    evaluation_path = artifact_dir / "synthaml-phase11-evaluation.json"
    save_model_bundle(model_path, estimator, fitted, scale, FEATURE_NAMES)
    document = _document(
        built_fingerprint=built.fingerprint,
        grouped_read=built.grouped_read,
        counts=counts,
        fitted=fitted,
        scale_mean=scale.mean,
        scale_spread=scale.scale,
        comparisons=comparisons,
        test_ids=test_ids,
        test_dates=test_dates,
        test_scores=test_scores,
        outcomes=outcomes,
        alert_sha256=file_sha256(alerts_path),
        transaction_sha256=built.transaction_sha256,
        feature_seconds=feature_seconds,
        fit_seconds=fit_seconds,
        total_seconds=time.perf_counter() - started,
    )
    temporary = evaluation_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
    temporary.replace(evaluation_path)
    return ExperimentResult(
        experiment_id=EXPERIMENT_ID,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        algorithm_family=ALGORITHM_FAMILY,
        feature_set_name=FEATURE_SET_NAME,
        feature_set_version=FEATURE_SET_VERSION,
        random_state=RANDOM_SEED,
        sklearn_version=fitted.sklearn_version,
        solver=SOLVER,
        logistic_c=LOGISTIC_C,
        max_iter=MAX_ITER,
        n_iter=fitted.n_iter,
        converged=fitted.converged,
        intercept=fitted.intercept,
        coefficient_count=len(fitted.coefficients),
        train_alerts=counts.train_alerts,
        test_alerts=counts.test_alerts,
        train_reports=counts.train_reports,
        train_dismisses=counts.train_alerts - counts.train_reports,
        test_reports=counts.test_reports,
        test_dismisses=counts.test_alerts - counts.test_reports,
        transaction_rows=counts.transaction_rows,
        future_rows=counts.future_rows,
        older_than_365_rows=counts.older_than_365_rows,
        feature_seconds=feature_seconds,
        fit_seconds=fit_seconds,
        total_seconds=time.perf_counter() - started,
        alert_sha256=document["alert_sha256"],
        transaction_sha256=built.transaction_sha256,
        feature_matrix_sha256=built.fingerprint,
        grouped_read=built.grouped_read,
        comparisons=comparisons,
        model_path=str(model_path),
        evaluation_path=str(evaluation_path),
        limitations=LIMITATIONS,
    )


def format_report(result: ExperimentResult) -> str:
    """Text report. Every figure is labeled as the SynthAML research benchmark."""

    lines = [
        f"SynthAML research benchmark {result.experiment_id}",
        (
            "These figures are results on the published synthetic dataset. "
            "They are not measured results for a financial institution."
        ),
        "",
        (
            f"Training alerts in 2020: {result.train_alerts} "
            f"({result.train_reports} Report, {result.train_dismisses} Dismiss)."
        ),
        (
            f"Held-out test alerts in 2021: {result.test_alerts} "
            f"({result.test_reports} Report, {result.test_dismisses} Dismiss)."
        ),
        (
            f"Transactions read: {result.transaction_rows}. "
            f"Rows after the published alert date: {result.future_rows}. "
            f"Rows more than 365 days before the alert date: {result.older_than_365_rows}."
        ),
        (
            f"Feature set {result.feature_set_name} version {result.feature_set_version}: "
            f"{result.coefficient_count} columns. The transaction file was read "
            f"{'in AlertID groups' if result.grouped_read else 'and sorted by AlertID'}."
        ),
        "",
        (
            f"Model {result.model_name} version {result.model_version}, "
            f"{result.algorithm_family}, L2, C={result.logistic_c:g}, solver {result.solver}, "
            f"max_iter {result.max_iter}, seed {result.random_state}."
        ),
        (
            f"scikit-learn {result.sklearn_version}. Iterations {result.n_iter}. "
            f"Converged: {'yes' if result.converged else 'no'}."
        ),
        f"Intercept: {result.intercept:.8f}.",
        SCORE_SEMANTICS,
        "",
        (
            "Review capacity is (test alerts * percent) // 100. "
            "The declared capacities are 5, 10, and 20 percent. "
            "The same number of alerts is reviewed under both orders."
        ),
        CHRONOLOGY_LIMIT,
        "",
        (
            "Capacity  Reviewed  Policy           Reports  Precision   Recall      "
            "Remaining  ML minus baseline reports"
        ),
    ]
    for comparison in result.comparisons:
        lines.append(_policy_line(comparison, "Date then AlertID", comparison.chronological, ""))
        lines.append(
            _policy_line(
                comparison,
                "Supervised rank",
                comparison.supervised,
                str(comparison.reports_identified_ml_minus_baseline),
            )
        )
    lines.extend(
        [
            "",
            "Positive ML minus baseline means the supervised rank identified more Report alerts.",
            "",
            (
                f"Feature build {result.feature_seconds:.3f} seconds. "
                f"Fit and score {result.fit_seconds:.3f} seconds. "
                f"Total {result.total_seconds:.3f} seconds."
            ),
            f"Alert file sha256: {result.alert_sha256}",
            f"Transaction file sha256: {result.transaction_sha256}",
            f"Unscaled feature fingerprint: {result.feature_matrix_sha256}",
            f"Model artifact: {result.model_path}",
            f"Evaluation artifact: {result.evaluation_path}",
            "",
            "Limitations:",
        ]
    )
    lines.extend(f"- {item}" for item in result.limitations)
    return "\n".join(lines)


def _year_masks(dates: tuple[date, ...]) -> tuple[np.ndarray, np.ndarray]:
    train = np.fromiter((item.year == 2020 for item in dates), dtype=bool, count=len(dates))
    test = np.fromiter((item.year == 2021 for item in dates), dtype=bool, count=len(dates))
    if int(train.sum() + test.sum()) != len(dates):
        raise SynthamlError("An alert date falls outside the 2020 training year and the 2021 test year.")
    return train, test


def _policy_line(comparison: CapacityComparison, policy: str, metrics, delta: str) -> str:
    return (
        f"{comparison.percent:>3}%      "
        f"{metrics.alerts_reviewed:>8}  "
        f"{policy:<16} "
        f"{metrics.reports_identified:>7}  "
        f"{metrics.precision_at_k:>10.6f}  "
        f"{metrics.recall_at_k:>10.6f}  "
        f"{metrics.reports_remaining:>9}  "
        f"{delta:>6}"
    )


def _document(
    *,
    built_fingerprint: str,
    grouped_read: bool,
    counts: PopulationCounts,
    fitted,
    scale_mean: tuple[float, ...],
    scale_spread: tuple[float, ...],
    comparisons: tuple[CapacityComparison, ...],
    test_ids: tuple[int, ...],
    test_dates: tuple[date, ...],
    test_scores: np.ndarray,
    outcomes: dict[int, str],
    alert_sha256: str,
    transaction_sha256: str,
    feature_seconds: float,
    fit_seconds: float,
    total_seconds: float,
) -> dict:
    from amlog.synthaml.metrics import chronological_order, model_order

    scores = tuple(float(score) for score in test_scores)
    chrono = chronological_order(test_ids, test_dates)
    ranked = model_order(test_ids, scores)
    chrono_position = {alert_id: position + 1 for position, alert_id in enumerate(chrono)}
    model_position = {alert_id: position + 1 for position, alert_id in enumerate(ranked)}
    score_by_id = dict(zip(test_ids, scores, strict=True))
    return {
        "experiment_id": EXPERIMENT_ID,
        "research_benchmark": "SynthAML",
        "institution_result": False,
        "paper_doi": PAPER_DOI,
        "feature_set_name": FEATURE_SET_NAME,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "entry_definition": ENTRY_DEFINITION,
        "size_definition": SIZE_DEFINITION,
        "missing_group_rule": MISSING_GROUP_RULE,
        "score_semantics": SCORE_SEMANTICS,
        "standard_deviation": "population, divide by N",
        "split": "train dates in 2020; test dates in 2021; boundary 2021-01-01",
        "chronology_limit": CHRONOLOGY_LIMIT,
        "limitations": list(LIMITATIONS),
        "grouped_read": grouped_read,
        "counts": counts.__dict__,
        "model": {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "algorithm_family": ALGORITHM_FAMILY,
            "solver": SOLVER,
            "penalty": "l2",
            "C": LOGISTIC_C,
            "max_iter": MAX_ITER,
            "random_state": RANDOM_SEED,
            "sklearn_version": fitted.sklearn_version,
            "n_iter": fitted.n_iter,
            "converged": fitted.converged,
            "intercept": fitted.intercept,
            "coefficients": list(fitted.coefficients),
            "positive_class": "Report",
        },
        "preprocessing": {
            "fit_on": "2020 training alerts only",
            "mean": list(scale_mean),
            "scale": list(scale_spread),
        },
        "alert_sha256": alert_sha256,
        "transaction_sha256": transaction_sha256,
        "feature_matrix_sha256": built_fingerprint,
        "timings_seconds": {
            "features": feature_seconds,
            "fit_and_score": fit_seconds,
            "total": total_seconds,
        },
        "comparisons": [
            {
                "percent": item.percent,
                "alerts_reviewed": item.alerts_reviewed,
                "test_reports": item.test_reports,
                "chronological": item.chronological.__dict__,
                "supervised": item.supervised.__dict__,
                "reports_identified_ml_minus_baseline": item.reports_identified_ml_minus_baseline,
                "precision_ml_minus_baseline": item.precision_ml_minus_baseline,
                "recall_ml_minus_baseline": item.recall_ml_minus_baseline,
                "reports_remaining_ml_minus_baseline": item.reports_remaining_ml_minus_baseline,
            }
            for item in comparisons
        ],
        "test_predictions": [
            {
                "alert_id": alert_id,
                "date": test_dates[index].isoformat(),
                "score": score_by_id[alert_id],
                "outcome": outcomes[alert_id],
                "chronological_position": chrono_position[alert_id],
                "model_position": model_position[alert_id],
            }
            for index, alert_id in enumerate(test_ids)
        ],
    }

"""Select a SynthAML ranker on 2020 quarters, then score 2021 once.

The command reuses the published feature builder. It does not open a database,
and it does not replace the Phase 11 logistic artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import joblib
import numpy as np

from amlog.synthaml.dataset import file_sha256, load_alert_dates, load_outcomes
from amlog.synthaml.errors import SynthamlError, SynthamlLeakageError
from amlog.synthaml.experiment import assert_published_population
from amlog.synthaml.experiment import PopulationCounts
from amlog.synthaml.features import build_feature_matrix
from amlog.synthaml.names import FEATURE_NAMES, FEATURE_SET_NAME, FEATURE_SET_VERSION, RANDOM_SEED
from amlog.synthaml.preprocess import apply_training_scale, fit_training_scale
from amlog.synthaml.run import default_artifact_dir
from amlog.synthaml.selection import (
    CANDIDATES,
    DECLARED_SETTINGS,
    HOLDOUT_DISCLOSURE,
    PRACTICAL_TIE_REPORTS,
    PRIMARY_METRIC,
    PRIMARY_PERCENT,
    SELECTION_EXPERIMENT_ID,
    SELECTION_FOLDS,
    FoldObservation,
    SelectionDecision,
    choose,
    fit_candidate,
    forward_fold_masks,
    period_metrics,
    report_scores,
    score_fold,
)

CACHE_NAME = "feature-cache-v1.npz"
SELECTION_JSON = "synthaml-phase11c-selection.json"
SELECTION_MODEL = "synthaml-phase11c-selected.joblib"
BASELINE_JSON = "synthaml-phase11-evaluation.json"
OVERFIT_GAP = 0.20

COMPLEXITY = {
    "date_order": (
        "No fitted parameters. Review follows the published date, then AlertID. "
        "Inference is a sort."
    ),
    "logistic_regression": (
        "56 coefficients and one intercept. Scoring is one dot product per alert. "
        "The coefficients can be read directly. This candidate uses the Phase 11 settings."
    ),
    "random_forest": (
        "200 trees, depth at most 16, at least 5 training alerts per leaf, "
        "balanced class weights. Scoring averages the trees. The artifact is larger "
        "than the linear model."
    ),
    "extra_trees": (
        "200 extremely randomized trees with the same depth, leaf, and class-weight "
        "limits as the forest. Scoring cost is similar to the forest."
    ),
    "hist_gradient_boosting": (
        "200 boosting iterations, depth at most 6, no internal validation split. "
        "Class balance uses weights from the training rows only. The artifact is "
        "smaller than the forests."
    ),
    "equal_score_ensemble": (
        "Unweighted mean of the four member scores. No weight was fit. "
        "Inference runs all four models."
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare declared SynthAML rankers on 2020 forward quarters, lock one "
            "candidate, then score 2021. This command does not open an AMLOG database."
        )
    )
    parser.add_argument("--alerts", type=Path)
    parser.add_argument("--transactions", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        from amlog.synthaml.dataset import resolve_alert_file, resolve_transaction_file

        alerts = resolve_alert_file(args.alerts)
        transactions = resolve_transaction_file(args.transactions)
        document = run_selection(
            alerts,
            transactions,
            args.artifact_dir or default_artifact_dir(),
            progress=_progress,
        )
    except SynthamlError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_selection_report(document))
    return 0


def run_selection(
    alerts_path: Path,
    transactions_path: Path,
    artifact_dir: Path,
    *,
    progress=None,
    published_checks: bool = True,
) -> dict:
    """Lock a candidate on 2020, then evaluate the locked candidates on 2021."""

    started = time.perf_counter()
    population = _load_population(alerts_path, transactions_path, artifact_dir, progress)
    train = population["years"] == 2020
    test = population["years"] == 2021
    if published_checks:
        _assert_counts(population, train, test)
    decision, observations, date_order = select_on_2020(
        population["dates"][train],
        population["alert_ids"][train],
        population["report_codes"][train],
        population["matrix"][train],
    )
    overfit = _overfit_notes(observations)
    holdout = evaluate_holdout_after_lock(
        decision,
        population["matrix"],
        population["report_codes"],
        population["alert_ids"],
        population["dates"],
        train,
        test,
        population["outcomes"],
    )
    baseline = _read_phase11_baseline(artifact_dir / BASELINE_JSON)
    document = {
        "experiment_id": SELECTION_EXPERIMENT_ID,
        "research_benchmark": "SynthAML",
        "institution_result": False,
        "approved_for_operational_use": False,
        "score_use": "experiment",
        "primary_metric": PRIMARY_METRIC,
        "primary_percent": PRIMARY_PERCENT,
        "practical_tie_reports": PRACTICAL_TIE_REPORTS,
        "selection_folds": list(SELECTION_FOLDS),
        "selection_locked_before_holdout": True,
        "holdout_disclosure": HOLDOUT_DISCLOSURE,
        "random_state": RANDOM_SEED,
        "feature_set_name": FEATURE_SET_NAME,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_matrix_sha256": population["fingerprint"],
        "alert_sha256": population["alert_sha256"],
        "transaction_sha256": population["transaction_sha256"],
        "declared_settings": DECLARED_SETTINGS,
        "complexity": COMPLEXITY,
        "selected_candidate": decision.candidate,
        "selection_reason": decision.reason,
        "validation_totals": _totals(observations),
        "date_order_validation": date_order,
        "validation_folds": [asdict(row) for row in observations],
        "overfit_notes": overfit,
        "holdout_2021": holdout["metrics"],
        "selected_sample": holdout["sample"],
        "phase11_logistic_baseline": baseline,
        "timings_seconds": {
            "features": population["feature_seconds"],
            "selection_and_holdout": time.perf_counter() - started - population["feature_seconds"],
            "total": time.perf_counter() - started,
        },
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / SELECTION_MODEL
    _save_selected_model(model_path, decision.candidate, holdout["scale"], holdout["members"])
    document["model_path"] = str(model_path)
    evaluation_path = artifact_dir / SELECTION_JSON
    temporary = evaluation_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
    temporary.replace(evaluation_path)
    document["evaluation_path"] = str(evaluation_path)
    return document


def select_on_2020(
    dates: np.ndarray,
    alert_ids: np.ndarray,
    report_codes: np.ndarray,
    matrix: np.ndarray,
) -> tuple[SelectionDecision, list[FoldObservation], list[dict]]:
    """Fit and choose using 2020 rows only. A later year cannot enter this function."""

    python_dates = tuple(_as_date(item) for item in dates)
    if any(day.year != 2020 for day in python_dates):
        raise SynthamlLeakageError("Algorithm selection received a date outside 2020.")
    observations: list[FoldObservation] = []
    date_order: list[dict] = []
    for fold_name, train, valid in forward_fold_masks(python_dates):
        rows, _members, _scale = score_fold(
            matrix,
            report_codes,
            alert_ids,
            python_dates,
            train,
            valid,
            fold_name,
        )
        observations.extend(rows)
        ordinals = np.asarray([day.toordinal() for day in python_dates], dtype=np.float64)
        metrics = period_metrics(-ordinals[valid], report_codes[valid], alert_ids[valid])
        metrics["candidate"] = "date_order"
        metrics["fold"] = fold_name
        date_order.append(metrics)
    decision = choose(observations)
    return decision, observations, date_order


def evaluate_holdout_after_lock(
    decision: SelectionDecision,
    matrix: np.ndarray,
    report_codes: np.ndarray,
    alert_ids: np.ndarray,
    dates: np.ndarray,
    train: np.ndarray,
    test: np.ndarray,
    outcomes: dict[int, str],
) -> dict:
    """Score 2021 after the candidate has already been named. This does not call choose."""

    if decision.candidate not in CANDIDATES:
        raise SynthamlError("The locked candidate is not one of the declared candidates.")
    scale = fit_training_scale(matrix[train])
    scaled = apply_training_scale(matrix, scale)
    members = {}
    scores = {}
    fit_seconds = {}
    for name in CANDIDATES:
        if name == "equal_score_ensemble":
            continue
        started = time.perf_counter()
        members[name] = fit_candidate(name, scaled[train], report_codes[train])
        scores[name] = report_scores(members[name], scaled[test])
        fit_seconds[name] = time.perf_counter() - started
    member_names = [name for name in CANDIDATES if name != "equal_score_ensemble"]
    scores["equal_score_ensemble"] = np.mean(
        np.vstack([scores[name] for name in member_names]),
        axis=0,
    )
    test_ids = alert_ids[test]
    test_codes = report_codes[test]
    test_dates = dates[test]
    rows = []
    ordinals = np.asarray([_as_date(item).toordinal() for item in test_dates], dtype=np.float64)
    date_metrics = period_metrics(-ordinals, test_codes, test_ids)
    date_metrics["candidate"] = "date_order"
    date_metrics["fit_seconds"] = 0.0
    rows.append(date_metrics)
    for name in CANDIDATES:
        metrics = period_metrics(scores[name], test_codes, test_ids)
        metrics["candidate"] = name
        metrics["fit_seconds"] = fit_seconds.get(name, sum(fit_seconds.values()))
        metrics["selected"] = name == decision.candidate
        rows.append(metrics)
    order = np.lexsort((test_ids, -scores[decision.candidate]))
    sample = []
    for rank, index in enumerate(order[:8], start=1):
        alert_id = int(test_ids[index])
        sample.append(
            {
                "rank": rank,
                "alert_id": alert_id,
                "score": float(scores[decision.candidate][index]),
                "outcome": outcomes[alert_id],
            }
        )
    return {"metrics": rows, "sample": sample, "scale": scale, "members": members, "scores": scores}


def format_selection_report(document: dict) -> str:
    """Text comparison. The selected name is the one locked on 2020."""

    lines = [
        f"SynthAML model selection {document['experiment_id']}",
        "These figures are results on the published synthetic dataset.",
        document["holdout_disclosure"],
        "",
        f"Primary metric: {document['primary_metric']}.",
        f"Selected candidate: {document['selected_candidate']}.",
        document["selection_reason"],
        "",
        "2020 validation, total Report outcomes at 10 percent:",
        f"{'candidate':<28} {'reports_at_10':>13} {'precision_std':>14} {'mean_pr_auc':>12}",
    ]
    for row in document["validation_totals"]:
        lines.append(
            f"{row['candidate']:<28} {row['reports_at_10']:>13} "
            f"{row['precision_at_10_std']:>14.6f} {row['average_precision_mean']:>12.6f}"
        )
    date_total = sum(row["reports_at_10"] for row in document["date_order_validation"])
    lines.append(f"{'date_order':<28} {date_total:>13}")
    lines.append("")
    lines.append("2021 held-out comparison, computed after the selection was locked:")
    lines.append(
        f"{'candidate':<28} {'at_5':>6} {'at_10':>6} {'at_20':>6} "
        f"{'precision_10':>12} {'recall_10':>10} {'pr_auc':>8}"
    )
    for row in document["holdout_2021"]:
        mark = " selected" if row.get("selected") else ""
        lines.append(
            f"{row['candidate']:<28} {row['reports_at_5']:>6} {row['reports_at_10']:>6} "
            f"{row['reports_at_20']:>6} {row['precision_at_10']:>12.6f} "
            f"{row['recall_at_10']:>10.6f} {row['average_precision']:>8.6f}{mark}"
        )
    if document["overfit_notes"]:
        lines.append("")
        lines.append("Training-to-validation gaps of 0.20 or more in precision at 10 percent:")
        lines.extend(document["overfit_notes"])
    lines.append("")
    lines.append("The selected model is an experiment. It is not approved for operational use.")
    return "\n".join(lines)


def _load_population(alerts_path: Path, transactions_path: Path, artifact_dir: Path, progress) -> dict:
    alert_dates = load_alert_dates(alerts_path)
    outcomes = load_outcomes(alerts_path)
    if set(alert_dates) != set(outcomes):
        raise SynthamlError("Alert dates and outcomes do not describe the same alerts.")
    cache = artifact_dir / CACHE_NAME
    feature_seconds = 0.0
    if cache.is_file():
        stored = np.load(cache, allow_pickle=False)
        alert_ids = stored["alert_ids"]
        ordinals = stored["date_ordinals"]
        matrix = stored["matrix"]
        fingerprint = str(stored["fingerprint"])
        transaction_sha256 = str(stored["transaction_sha256"])
        transaction_rows = int(stored["transaction_rows"])
        future_rows = int(stored["future_rows"])
        older = int(stored["older_than_365_rows"])
        missing = int(stored["alerts_without_transactions"])
    else:
        started = time.perf_counter()
        built = build_feature_matrix(alert_dates, transactions_path, progress)
        feature_seconds = time.perf_counter() - started
        if built.future_rows:
            raise SynthamlLeakageError(
                f"{built.future_rows} transactions are dated after their alert's published date."
            )
        alert_ids = np.asarray(built.alert_ids, dtype=np.int64)
        ordinals = np.asarray([day.toordinal() for day in built.alert_dates], dtype=np.int64)
        matrix = np.ascontiguousarray(built.matrix, dtype=np.float64)
        fingerprint = built.fingerprint
        transaction_sha256 = built.transaction_sha256
        transaction_rows = built.transaction_rows
        future_rows = built.future_rows
        older = built.older_than_365_rows
        missing = built.alerts_without_transactions
        artifact_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            cache,
            alert_ids=alert_ids,
            date_ordinals=ordinals,
            matrix=matrix,
            fingerprint=np.asarray(fingerprint),
            transaction_sha256=np.asarray(transaction_sha256),
            transaction_rows=np.asarray(transaction_rows),
            future_rows=np.asarray(future_rows),
            older_than_365_rows=np.asarray(older),
            alerts_without_transactions=np.asarray(missing),
        )
    if matrix.shape != (len(alert_ids), len(FEATURE_NAMES)):
        raise SynthamlError("The cached feature matrix does not have the published 56 columns.")
    dates = np.asarray([date.fromordinal(int(value)) for value in ordinals], dtype=object)
    years = np.asarray([day.year for day in dates], dtype=np.int64)
    report_codes = np.asarray(
        [1 if outcomes[int(alert_id)] == "Report" else 0 for alert_id in alert_ids],
        dtype=np.int64,
    )
    return {
        "alert_ids": np.asarray(alert_ids, dtype=np.int64),
        "dates": dates,
        "years": years,
        "report_codes": report_codes,
        "matrix": np.asarray(matrix, dtype=np.float64),
        "outcomes": outcomes,
        "fingerprint": fingerprint,
        "alert_sha256": file_sha256(alerts_path),
        "transaction_sha256": transaction_sha256,
        "transaction_rows": transaction_rows,
        "future_rows": future_rows,
        "older_than_365_rows": older,
        "alerts_without_transactions": missing,
        "feature_seconds": feature_seconds,
    }


def _assert_counts(population: dict, train: np.ndarray, test: np.ndarray) -> None:
    counts = PopulationCounts(
        alerts=int(len(population["alert_ids"])),
        train_alerts=int(train.sum()),
        test_alerts=int(test.sum()),
        reports=int(population["report_codes"].sum()),
        dismissals=int(len(population["report_codes"]) - population["report_codes"].sum()),
        train_reports=int(population["report_codes"][train].sum()),
        test_reports=int(population["report_codes"][test].sum()),
        transaction_rows=int(population["transaction_rows"]),
        future_rows=int(population["future_rows"]),
        older_than_365_rows=int(population["older_than_365_rows"]),
        alerts_without_transactions=int(population["alerts_without_transactions"]),
    )
    assert_published_population(counts)


def _totals(observations: list[FoldObservation]) -> list[dict]:
    rows = []
    for candidate in CANDIDATES:
        subset = [row for row in observations if row.candidate == candidate]
        precision = np.asarray([row.precision_at_10 for row in subset], dtype=np.float64)
        curves = np.asarray([row.average_precision for row in subset], dtype=np.float64)
        rows.append(
            {
                "candidate": candidate,
                "reports_at_10": int(sum(row.reports_at_10 for row in subset)),
                "reports_at_5": int(sum(row.reports_at_5 for row in subset)),
                "reports_at_20": int(sum(row.reports_at_20 for row in subset)),
                "precision_at_10_mean": float(precision.mean()),
                "precision_at_10_std": float(precision.std(ddof=0)),
                "recall_at_10_mean": float(np.mean([row.recall_at_10 for row in subset])),
                "average_precision_mean": float(curves.mean()),
            }
        )
    return rows


def _overfit_notes(observations: list[FoldObservation]) -> list[str]:
    notes = []
    for row in observations:
        gap = row.train_precision_at_10 - row.precision_at_10
        if gap >= OVERFIT_GAP:
            notes.append(
                f"{row.candidate} on {row.fold}: training precision at 10 percent is "
                f"{row.train_precision_at_10:.3f} and validation precision is "
                f"{row.precision_at_10:.3f}."
            )
    return notes


def _save_selected_model(path: Path, selected: str, scale, members: dict) -> None:
    stored_members = members if selected == "equal_score_ensemble" else {selected: members[selected]}
    bundle = {
        "format_version": 1,
        "experiment_id": SELECTION_EXPERIMENT_ID,
        "selected_candidate": selected,
        "feature_names": list(FEATURE_NAMES),
        "mean": list(scale.mean),
        "scale": list(scale.scale),
        "members": stored_members,
        "declared_settings": DECLARED_SETTINGS[selected],
        "approved_for_operational_use": False,
        "score_use": "experiment",
        "random_state": RANDOM_SEED,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".joblib.tmp")
    joblib.dump(bundle, temporary)
    temporary.replace(path)


def _read_phase11_baseline(path: Path) -> dict | None:
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        "experiment_id": document.get("experiment_id"),
        "source": path.name,
        "note": "These 2021 figures were already inspected in Phase 11.",
        "comparisons": document.get("comparisons"),
    }


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    raise SynthamlError("Alert dates must be calendar dates.")


def _progress(rows: int) -> None:
    print(f"transactions_read {rows}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

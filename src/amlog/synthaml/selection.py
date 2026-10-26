"""Choose a SynthAML ranking model from 2020 quarters only.

The primary metric is the number of Report outcomes inside a 10 percent
review of each forward validation quarter. Accuracy is not used. The 2021
holdout is not an input to this choice.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

from amlog.synthaml.errors import SynthamlError, SynthamlLeakageError
from amlog.synthaml.names import LOGISTIC_C, MAX_ITER, RANDOM_SEED, SOLVER
from amlog.synthaml.preprocess import TrainingScale, apply_training_scale, fit_training_scale

SELECTION_EXPERIMENT_ID = "phase11c-synthaml-v1"
PRIMARY_METRIC = "reports_captured_at_10_percent"
PRIMARY_PERCENT = 10
SECONDARY_PERCENTS = (5, 10, 20)
SELECTION_FOLDS = ("2020Q2", "2020Q3", "2020Q4")
# Set before any 2020 fit. Candidates inside this many reports of the leader
# are treated as equally effective, and the stabler then simpler one is kept.
PRACTICAL_TIE_REPORTS = 10
CANDIDATES = (
    "logistic_regression",
    "random_forest",
    "hist_gradient_boosting",
    "extra_trees",
    "equal_score_ensemble",
)
# Lower is simpler. Used only when the primary metric and stability are tied.
SIMPLICITY = {
    "logistic_regression": 0,
    "extra_trees": 1,
    "random_forest": 2,
    "hist_gradient_boosting": 3,
    "equal_score_ensemble": 4,
}
TREE_ESTIMATORS = 200
TREE_MAX_DEPTH = 16
TREE_MIN_SAMPLES_LEAF = 5
HGB_MAX_ITER = 200
HGB_LEARNING_RATE = 0.1
HGB_MAX_DEPTH = 6

DECLARED_SETTINGS = {
    "logistic_regression": {
        "C": LOGISTIC_C,
        "penalty": "l2",
        "solver": SOLVER,
        "max_iter": MAX_ITER,
        "class_weight": None,
        "random_state": RANDOM_SEED,
    },
    "random_forest": {
        "n_estimators": TREE_ESTIMATORS,
        "max_depth": TREE_MAX_DEPTH,
        "min_samples_leaf": TREE_MIN_SAMPLES_LEAF,
        "class_weight": "balanced",
        "random_state": RANDOM_SEED,
        "n_jobs": 1,
    },
    "extra_trees": {
        "n_estimators": TREE_ESTIMATORS,
        "max_depth": TREE_MAX_DEPTH,
        "min_samples_leaf": TREE_MIN_SAMPLES_LEAF,
        "class_weight": "balanced",
        "random_state": RANDOM_SEED,
        "n_jobs": 1,
    },
    "hist_gradient_boosting": {
        "max_iter": HGB_MAX_ITER,
        "learning_rate": HGB_LEARNING_RATE,
        "max_depth": HGB_MAX_DEPTH,
        "early_stopping": False,
        "class_balance": "training-fold sample weights",
        "random_state": RANDOM_SEED,
    },
    "equal_score_ensemble": {
        "members": list(CANDIDATES[:-1]),
        "weights": "equal mean of member Report scores",
        "weight_fitting": False,
    },
}

HOLDOUT_DISCLOSURE = (
    "The 2021 Report and Dismiss outcomes were inspected when the Phase 11 "
    "logistic regression baseline was evaluated. Algorithm selection for this "
    "comparison used only 2020 forward quarters. The 2021 scores were computed "
    "after that choice was locked and were not used to change it."
)


@dataclass(frozen=True)
class FoldObservation:
    """One candidate on one 2020 validation quarter."""

    candidate: str
    fold: str
    alerts: int
    reports: int
    reports_at_5: int
    reports_at_10: int
    reports_at_20: int
    precision_at_5: float
    precision_at_10: float
    precision_at_20: float
    recall_at_5: float
    recall_at_10: float
    recall_at_20: float
    average_precision: float
    train_precision_at_10: float


@dataclass(frozen=True)
class SelectionDecision:
    """The candidate named by the declared rule. It does not contain 2021 scores."""

    candidate: str
    total_reports_at_10: int
    precision_at_10_std: float
    reason: str


def quarter_index(day: date) -> int:
    """Map a 2020 date to quarter 0, 1, 2, or 3. Any other year is rejected."""

    if day.year != 2020:
        raise SynthamlLeakageError(
            f"{day.isoformat()} is outside the 2020 selection year."
        )
    return (day.month - 1) // 3


def forward_fold_masks(dates: Sequence[date]) -> tuple[tuple[str, np.ndarray, np.ndarray], ...]:
    """Three expanding folds. Each validation quarter is strictly after its training rows."""

    quarters = np.asarray([quarter_index(day) for day in dates], dtype=np.int64)
    ordinals = np.asarray([day.toordinal() for day in dates], dtype=np.int64)
    folds = []
    for validate_quarter, fold_name in ((1, "2020Q2"), (2, "2020Q3"), (3, "2020Q4")):
        train = quarters < validate_quarter
        valid = quarters == validate_quarter
        if not bool(train.any()) or not bool(valid.any()):
            raise SynthamlError(f"{fold_name} does not have both a past and a validation quarter.")
        if int(ordinals[train].max()) >= int(ordinals[valid].min()):
            raise SynthamlLeakageError(
                f"{fold_name} training dates are not strictly before the validation quarter."
            )
        folds.append((fold_name, train, valid))
    return tuple(folds)


def reports_at_percent(
    scores: np.ndarray,
    report_codes: np.ndarray,
    alert_ids: np.ndarray,
    percent: int,
) -> tuple[int, int, float, float]:
    """Report count, K, precision, and recall at a predeclared whole percent."""

    if percent not in SECONDARY_PERCENTS:
        raise SynthamlError("That capacity was not part of the declared evaluation.")
    labels = np.asarray(report_codes, dtype=np.int64)
    total_reports = int(labels.sum())
    if total_reports < 1:
        raise SynthamlError("Recall is undefined when the period has no Report outcomes.")
    k = (len(labels) * percent) // 100
    if k < 1:
        raise SynthamlError("Review capacity is zero for this period.")
    order = np.lexsort((np.asarray(alert_ids), -np.asarray(scores, dtype=np.float64)))
    captured = int(labels[order[:k]].sum())
    return captured, k, captured / k, captured / total_reports


def average_precision(report_codes: np.ndarray, scores: np.ndarray) -> float:
    """Area under the precision-recall curve. This is not accuracy."""

    return float(average_precision_score(np.asarray(report_codes), np.asarray(scores)))


def choose(observations: Sequence[FoldObservation]) -> SelectionDecision:
    """Name one candidate from 2020 fold rows. Fold names outside that set are rejected."""

    if not observations:
        raise SynthamlError("There are no validation observations to choose from.")
    grouped: dict[str, list[FoldObservation]] = defaultdict(list)
    for row in observations:
        if row.fold not in SELECTION_FOLDS:
            raise SynthamlLeakageError(
                f"Fold {row.fold} is not one of the declared 2020 validation quarters."
            )
        if row.candidate not in CANDIDATES:
            raise SynthamlError(f"{row.candidate} is not one of the declared candidates.")
        grouped[row.candidate].append(row)
    ranked = []
    for candidate in CANDIDATES:
        rows = grouped.get(candidate, [])
        present = {row.fold for row in rows}
        if present != set(SELECTION_FOLDS):
            raise SynthamlError(f"{candidate} does not have every 2020 validation quarter.")
        total = sum(row.reports_at_10 for row in rows)
        precision = np.asarray([row.precision_at_10 for row in rows], dtype=np.float64)
        stability = float(precision.std(ddof=0))
        ranked.append((total, stability, SIMPLICITY[candidate], candidate))
    leader_total = max(item[0] for item in ranked)
    leader = min(
        (item for item in ranked if item[0] == leader_total),
        key=lambda item: (item[1], item[2]),
    )[3]
    band = [item for item in ranked if leader_total - item[0] <= PRACTICAL_TIE_REPORTS]
    band.sort(key=lambda item: (item[1], item[2], -item[0]))
    total, stability, _simplicity, candidate = band[0]
    reason = (
        f"{candidate} is the selected candidate under the declared rule. "
        f"It captured {total} Report outcomes at 10 percent across {', '.join(SELECTION_FOLDS)}. "
        f"The population standard deviation of its precision at 10 percent is {stability:.6f}. "
        f"{leader} has the highest total, {leader_total}. "
        f"Candidates within {PRACTICAL_TIE_REPORTS} reports of that total are treated as equally effective, "
        "and the stabler then simpler candidate in that band is kept. "
        "This choice does not read 2021 outcomes."
    )
    return SelectionDecision(
        candidate=candidate,
        total_reports_at_10=total,
        precision_at_10_std=stability,
        reason=reason,
    )


def balanced_sample_weight(report_codes: np.ndarray) -> np.ndarray:
    """Inverse class frequency on the training rows of one fold."""

    labels = np.asarray(report_codes, dtype=np.int64)
    positive = int(labels.sum())
    negative = int(len(labels) - positive)
    if positive < 1 or negative < 1:
        raise SynthamlError("A training fold does not contain both outcomes.")
    weight = np.empty(len(labels), dtype=np.float64)
    weight[labels == 1] = len(labels) / (2.0 * positive)
    weight[labels == 0] = len(labels) / (2.0 * negative)
    return weight


def fit_candidate(name: str, matrix: np.ndarray, report_codes: np.ndarray):
    """Fit one declared candidate. The ensemble is not a separate fit."""

    features = np.ascontiguousarray(matrix, dtype=np.float64)
    labels = np.asarray(report_codes, dtype=np.int64)
    if name == "logistic_regression":
        model = LogisticRegression(
            C=LOGISTIC_C,
            solver=SOLVER,
            penalty="l2",
            max_iter=MAX_ITER,
            random_state=RANDOM_SEED,
        )
        model.fit(features, labels)
        return model
    if name == "random_forest":
        model = RandomForestClassifier(
            n_estimators=TREE_ESTIMATORS,
            max_depth=TREE_MAX_DEPTH,
            min_samples_leaf=TREE_MIN_SAMPLES_LEAF,
            class_weight="balanced",
            random_state=RANDOM_SEED,
            n_jobs=1,
        )
        model.fit(features, labels)
        return model
    if name == "extra_trees":
        model = ExtraTreesClassifier(
            n_estimators=TREE_ESTIMATORS,
            max_depth=TREE_MAX_DEPTH,
            min_samples_leaf=TREE_MIN_SAMPLES_LEAF,
            class_weight="balanced",
            random_state=RANDOM_SEED,
            n_jobs=1,
        )
        model.fit(features, labels)
        return model
    if name == "hist_gradient_boosting":
        model = HistGradientBoostingClassifier(
            max_iter=HGB_MAX_ITER,
            learning_rate=HGB_LEARNING_RATE,
            max_depth=HGB_MAX_DEPTH,
            early_stopping=False,
            random_state=RANDOM_SEED,
        )
        model.fit(features, labels, sample_weight=balanced_sample_weight(labels))
        return model
    raise SynthamlError(f"{name} is not a fitted candidate.")


def report_scores(model, matrix: np.ndarray) -> np.ndarray:
    """Report-class score. This does not fit the model."""

    features = np.ascontiguousarray(matrix, dtype=np.float64)
    positive = list(model.classes_).index(1)
    return np.asarray(model.predict_proba(features)[:, positive], dtype=np.float64)


def score_fold(
    matrix: np.ndarray,
    report_codes: np.ndarray,
    alert_ids: np.ndarray,
    dates: Sequence[date],
    train: np.ndarray,
    valid: np.ndarray,
    fold_name: str,
) -> tuple[list[FoldObservation], dict[str, object], TrainingScale]:
    """Fit every candidate on the past quarters and score the next quarter."""

    train_dates = [dates[index] for index in np.flatnonzero(train)]
    valid_dates = [dates[index] for index in np.flatnonzero(valid)]
    if max(train_dates) >= min(valid_dates):
        raise SynthamlLeakageError(
            f"{fold_name} would fit preprocessing on a date that is not before the validation quarter."
        )
    scale = fit_training_scale(matrix[train])
    scaled = apply_training_scale(matrix, scale)
    members = {}
    validation_scores = {}
    training_scores = {}
    for name in CANDIDATES:
        if name == "equal_score_ensemble":
            continue
        model = fit_candidate(name, scaled[train], report_codes[train])
        members[name] = model
        validation_scores[name] = report_scores(model, scaled[valid])
        training_scores[name] = report_scores(model, scaled[train])
    validation_scores["equal_score_ensemble"] = np.mean(
        np.vstack([validation_scores[name] for name in CANDIDATES if name != "equal_score_ensemble"]),
        axis=0,
    )
    training_scores["equal_score_ensemble"] = np.mean(
        np.vstack([training_scores[name] for name in CANDIDATES if name != "equal_score_ensemble"]),
        axis=0,
    )
    rows = []
    for name in CANDIDATES:
        rows.append(
            _observation(
                name,
                fold_name,
                validation_scores[name],
                report_codes[valid],
                alert_ids[valid],
                training_scores[name],
                report_codes[train],
                alert_ids[train],
            )
        )
    return rows, members, scale


def _observation(
    candidate: str,
    fold_name: str,
    scores: np.ndarray,
    report_codes: np.ndarray,
    alert_ids: np.ndarray,
    train_scores: np.ndarray,
    train_codes: np.ndarray,
    train_ids: np.ndarray,
) -> FoldObservation:
    captured = {}
    precision = {}
    recall = {}
    alerts = len(report_codes)
    for percent in SECONDARY_PERCENTS:
        count, _k, prec, rec = reports_at_percent(scores, report_codes, alert_ids, percent)
        captured[percent] = count
        precision[percent] = prec
        recall[percent] = rec
    _train_count, _train_k, train_precision, _train_recall = reports_at_percent(
        train_scores, train_codes, train_ids, PRIMARY_PERCENT
    )
    return FoldObservation(
        candidate=candidate,
        fold=fold_name,
        alerts=alerts,
        reports=int(np.asarray(report_codes).sum()),
        reports_at_5=captured[5],
        reports_at_10=captured[10],
        reports_at_20=captured[20],
        precision_at_5=precision[5],
        precision_at_10=precision[10],
        precision_at_20=precision[20],
        recall_at_5=recall[5],
        recall_at_10=recall[10],
        recall_at_20=recall[20],
        average_precision=average_precision(report_codes, scores),
        train_precision_at_10=train_precision,
    )


def period_metrics(
    scores: np.ndarray,
    report_codes: np.ndarray,
    alert_ids: np.ndarray,
) -> dict[str, float | int]:
    """Capacity metrics and PR-AUC for one already-scored period."""

    result: dict[str, float | int] = {
        "alerts": len(report_codes),
        "reports": int(np.asarray(report_codes).sum()),
        "average_precision": average_precision(report_codes, scores),
    }
    for percent in SECONDARY_PERCENTS:
        captured, k, precision, recall = reports_at_percent(scores, report_codes, alert_ids, percent)
        result[f"reviewed_at_{percent}"] = k
        result[f"reports_at_{percent}"] = captured
        result[f"precision_at_{percent}"] = precision
        result[f"recall_at_{percent}"] = recall
        result[f"remaining_at_{percent}"] = int(result["reports"]) - captured
    return result


def score_saved_selection(bundle: Mapping[str, object], matrix: np.ndarray) -> np.ndarray:
    """Score unscaled rows with the saved 2020 scale and the selected candidate."""

    scale = TrainingScale(
        mean=tuple(bundle["mean"]),
        scale=tuple(bundle["scale"]),
        train_rows=0,
        train_columns=len(bundle["mean"]),
    )
    scaled = apply_training_scale(matrix, scale)
    selected = str(bundle["selected_candidate"])
    members = bundle["members"]
    if selected == "equal_score_ensemble":
        stacked = np.vstack([report_scores(members[name], scaled) for name in CANDIDATES if name != "equal_score_ensemble"])
        return np.mean(stacked, axis=0)
    return report_scores(members[selected], scaled)

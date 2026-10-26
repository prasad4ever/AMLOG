"""One logistic regression for the SynthAML Report outcome.

The positive class is the published Report label. The score is that class's
predicted relative likelihood on this research benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from sklearn.linear_model import LogisticRegression

from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.names import (
    ALGORITHM_FAMILY,
    EXPERIMENT_ID,
    FEATURE_SET_NAME,
    FEATURE_SET_VERSION,
    LOGISTIC_C,
    MAX_ITER,
    MODEL_NAME,
    MODEL_VERSION,
    RANDOM_SEED,
    SCORE_SEMANTICS,
    SOLVER,
)
from amlog.synthaml.preprocess import TrainingScale, apply_training_scale


@dataclass(frozen=True)
class ReportModel:
    """Coefficients of one fitted baseline. Class 1 is Report."""

    coefficients: tuple[float, ...]
    intercept: float
    n_iter: int
    converged: bool
    sklearn_version: str
    random_state: int


def fit_report_model(matrix: np.ndarray, report_codes: np.ndarray) -> tuple[LogisticRegression, ReportModel]:
    """Fit the declared logistic regression. Both training classes are required."""

    features = np.ascontiguousarray(matrix, dtype=np.float64)
    labels = np.asarray(report_codes, dtype=np.int64)
    if features.shape[0] != labels.shape[0]:
        raise SynthamlError("Training features and training outcomes have different lengths.")
    classes = set(int(value) for value in labels.tolist())
    if classes != {0, 1}:
        raise SynthamlError("Training data does not contain both Report and Dismiss outcomes.")
    estimator = LogisticRegression(
        C=LOGISTIC_C,
        solver=SOLVER,
        penalty="l2",
        max_iter=MAX_ITER,
        random_state=RANDOM_SEED,
    )
    estimator.fit(features, labels)
    n_iter = int(np.max(estimator.n_iter_))
    coefficients = tuple(float(value) for value in np.asarray(estimator.coef_).ravel())
    return estimator, ReportModel(
        coefficients=coefficients,
        intercept=float(np.asarray(estimator.intercept_).ravel()[0]),
        n_iter=n_iter,
        converged=n_iter < MAX_ITER,
        sklearn_version=sklearn.__version__,
        random_state=RANDOM_SEED,
    )


def score_report_model(estimator: LogisticRegression, matrix: np.ndarray) -> np.ndarray:
    """Return the predicted relative likelihood of Report. This does not fit."""

    features = np.ascontiguousarray(matrix, dtype=np.float64)
    positive = list(estimator.classes_).index(1)
    return np.asarray(estimator.predict_proba(features)[:, positive], dtype=np.float64)


def save_model_bundle(
    path: Path,
    estimator: LogisticRegression,
    fitted: ReportModel,
    scale: TrainingScale,
    feature_names: tuple[str, ...],
) -> None:
    """Write the estimator and the training scale outside the feature values."""

    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "format_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "algorithm_family": ALGORITHM_FAMILY,
        "feature_set_name": FEATURE_SET_NAME,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_names": list(feature_names),
        "random_state": fitted.random_state,
        "solver": SOLVER,
        "C": LOGISTIC_C,
        "penalty": "l2",
        "max_iter": MAX_ITER,
        "n_iter": fitted.n_iter,
        "converged": fitted.converged,
        "sklearn_version": fitted.sklearn_version,
        "coefficients": list(fitted.coefficients),
        "intercept": fitted.intercept,
        "positive_class": "Report",
        "score_semantics": SCORE_SEMANTICS,
        "mean": list(scale.mean),
        "scale": list(scale.scale),
        "model": estimator,
    }
    temporary = path.with_suffix(".joblib.tmp")
    joblib.dump(bundle, temporary)
    temporary.replace(path)


def load_model_bundle(path: Path) -> dict[str, Any]:
    """Load a saved research model. The caller scores with ``score_saved_bundle``."""

    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or bundle.get("experiment_id") != EXPERIMENT_ID:
        raise SynthamlError("The saved artifact is not the SynthAML phase 11 model.")
    return bundle


def score_saved_bundle(bundle: dict[str, Any], matrix: np.ndarray) -> np.ndarray:
    """Score unscaled features with the saved training scale and estimator."""

    scale = TrainingScale(
        mean=tuple(bundle["mean"]),
        scale=tuple(bundle["scale"]),
        train_rows=0,
        train_columns=len(bundle["mean"]),
    )
    transformed = apply_training_scale(matrix, scale)
    return score_report_model(bundle["model"], transformed)

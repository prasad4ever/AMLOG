"""Scale SynthAML features with statistics fit on the training alerts only.

Missing groups already hold the published sentinel. This step does not replace
that sentinel with a median, and it does not read investigation outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from amlog.synthaml.errors import SynthamlError


@dataclass(frozen=True)
class TrainingScale:
    """Mean and population standard deviation of the training matrix."""

    mean: tuple[float, ...]
    scale: tuple[float, ...]
    train_rows: int
    train_columns: int


def fit_training_scale(matrix: np.ndarray) -> TrainingScale:
    """Fit column mean and population standard deviation. A constant column has scale 1."""

    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise SynthamlError("Training features must be a non-empty matrix.")
    if not np.isfinite(values).all():
        raise SynthamlError("Training features contain a non-finite value.")
    mean = values.mean(axis=0)
    spread = values.std(axis=0, ddof=0)
    spread = np.where(spread == 0.0, 1.0, spread)
    return TrainingScale(
        mean=tuple(float(item) for item in mean),
        scale=tuple(float(item) for item in spread),
        train_rows=int(values.shape[0]),
        train_columns=int(values.shape[1]),
    )


def apply_training_scale(matrix: np.ndarray, scale: TrainingScale) -> np.ndarray:
    """Apply a previously fit training scale. This does not refit it."""

    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != scale.train_columns:
        raise SynthamlError("The feature matrix does not match the training scale.")
    mean = np.asarray(scale.mean, dtype=np.float64)
    spread = np.asarray(scale.scale, dtype=np.float64)
    return (values - mean) / spread

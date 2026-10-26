"""Fit an Isolation Forest and a robust z-score baseline.

A higher score means more unusual. Neither score is a probability.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

from amlog.prioritize.names import N_ESTIMATORS, RANDOM_SEED
from amlog.prioritize.preprocess import PreparedMatrix, _median, _missing_name


@dataclass(frozen=True)
class RankedRow:
    """One alert after both rankings."""

    anomaly_score: float
    baseline_score: float
    anomaly_rank: int
    baseline_rank: int
    sample_role: str
    top_features: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Ranking:
    """Ranks for every prepared row, in the same order."""

    rows: tuple[RankedRow, ...]
    holdout_rank_agreement: float | None
    training_alerts: int
    holdout_alerts: int
    forest: IsolationForest
    baseline_spreads: tuple[tuple[str, float, float], ...]


def rank_alerts(
    prepared: PreparedMatrix,
    *,
    n_estimators: int = N_ESTIMATORS,
    random_state: int = RANDOM_SEED,
) -> Ranking:
    """Fit on the training rows and score every row, including the holdout."""

    matrix = np.asarray(prepared.rows, dtype=np.float64)
    training_index = [index for index, selected in enumerate(prepared.training_mask) if selected]
    training_matrix = matrix[training_index]
    forest = IsolationForest(
        n_estimators=n_estimators,
        max_samples=min(256, training_matrix.shape[0]),
        contamination="auto",
        random_state=random_state,
        bootstrap=False,
        n_jobs=1,
    )
    forest.fit(training_matrix)
    # decision_function is higher for ordinary rows. Negate it so a review
    # queue can sort descending and see the more unusual alerts first.
    anomaly = (-forest.decision_function(matrix)).tolist()
    spreads = _column_spreads(prepared, training_index)
    baseline = _baseline_scores(prepared, spreads)
    anomaly_rank = _ranks(anomaly)
    baseline_rank = _ranks(baseline)
    explanations = _top_features(prepared, spreads)
    rows = []
    for index in range(len(prepared.rows)):
        role = "training" if prepared.training_mask[index] else "holdout"
        rows.append(
            RankedRow(
                anomaly_score=float(anomaly[index]),
                baseline_score=float(baseline[index]),
                anomaly_rank=anomaly_rank[index],
                baseline_rank=baseline_rank[index],
                sample_role=role,
                top_features=explanations[index],
            )
        )
    holdout = [index for index, selected in enumerate(prepared.training_mask) if not selected]
    agreement = None
    if len(holdout) >= 2:
        agreement = _spearman(
            [anomaly[index] for index in holdout],
            [baseline[index] for index in holdout],
        )
    return Ranking(
        rows=tuple(rows),
        holdout_rank_agreement=agreement,
        training_alerts=len(training_index),
        holdout_alerts=len(holdout),
        forest=forest,
        baseline_spreads=tuple((name, center, spread) for name, center, spread, _index in spreads),
    )


def _baseline_scores(
    prepared: PreparedMatrix, usable: tuple[tuple[str, float, float, int], ...]
) -> list[float]:
    scores: list[float] = []
    for row in prepared.rows:
        distances = []
        for name, center, spread, index in usable:
            if _indicator_is_missing(prepared.columns, row, name):
                continue
            distances.append(abs((row[index] - center) / spread))
        scores.append(sum(distances) / len(distances) if distances else 0.0)
    return scores


def _top_features(
    prepared: PreparedMatrix, usable: tuple[tuple[str, float, float, int], ...]
) -> list[tuple[tuple[str, str], ...]]:
    explanations = []
    for row in prepared.rows:
        ranked = []
        for name, center, spread, index in usable:
            if _indicator_is_missing(prepared.columns, row, name):
                continue
            distance = (row[index] - center) / spread
            ranked.append((abs(distance), name, distance))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        explanations.append(tuple((name, f"{distance:.4f}") for _, name, distance in ranked[:5]))
    return explanations


def _column_spreads(
    prepared: PreparedMatrix, training_index: list[int]
) -> tuple[tuple[str, float, float, int], ...]:
    found = []
    for index, name in enumerate(prepared.columns):
        if name.endswith("__missing"):
            continue
        values = [prepared.rows[row_index][index] for row_index in training_index]
        center = _median(values)
        spread = _median([abs(value - center) for value in values])
        if spread <= 0:
            continue
        found.append((name, center, spread, index))
    return tuple(found)


def _indicator_is_missing(columns: tuple[str, ...], row: tuple[float, ...], name: str) -> bool:
    indicator = _missing_name(name)
    if indicator not in columns:
        return False
    return row[columns.index(indicator)] == 1.0


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ranks = [0] * len(scores)
    for position, index in enumerate(order, start=1):
        ranks[index] = position
    return ranks


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    return _pearson(left_ranks, right_ranks)


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average = (start + 1 + end) / 2.0
        for position in range(start, end):
            ranks[order[position]] = average
        start = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    count = len(left)
    left_mean = sum(left) / count
    right_mean = sum(right) / count
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_scale = sum((value - left_mean) ** 2 for value in left) ** 0.5
    right_scale = sum((value - right_mean) ** 2 for value in right) ** 0.5
    if left_scale == 0 or right_scale == 0:
        return None
    return numerator / (left_scale * right_scale)

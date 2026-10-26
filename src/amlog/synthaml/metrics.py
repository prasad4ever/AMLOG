"""Equal-capacity comparison on the held-out SynthAML alerts.

Review capacity is an integer percentage declared in ``names.CAPACITIES``.
The same K is applied to both orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.names import CAPACITIES, CHRONOLOGY_LIMIT


@dataclass(frozen=True)
class CapacityMetrics:
    """One review policy at one predeclared capacity."""

    alerts_reviewed: int
    reports_identified: int
    precision_at_k: float
    recall_at_k: float
    reports_remaining: int


@dataclass(frozen=True)
class CapacityComparison:
    """Chronological order and supervised order at the same K."""

    percent: int
    alerts_reviewed: int
    test_reports: int
    chronological: CapacityMetrics
    supervised: CapacityMetrics
    reports_identified_ml_minus_baseline: int
    precision_ml_minus_baseline: float
    recall_ml_minus_baseline: float
    reports_remaining_ml_minus_baseline: int
    chronology_limit: str = CHRONOLOGY_LIMIT


def review_capacity(alert_count: int, numerator: int, denominator: int) -> int:
    """Floor of a predeclared percentage. Other percentages are rejected."""

    if (numerator, denominator) not in CAPACITIES:
        raise SynthamlError("That review capacity was not part of the declared experiment.")
    if alert_count < 0:
        raise SynthamlError("Alert count cannot be negative.")
    return (alert_count * numerator) // denominator


def chronological_order(alert_ids: list[int] | tuple[int, ...], dates: list[date] | tuple[date, ...]) -> tuple[int, ...]:
    """Unprioritized order: published date, then numeric AlertID."""

    if len(alert_ids) != len(dates):
        raise SynthamlError("Dates and alert identifiers have different lengths.")
    ranked = sorted(range(len(alert_ids)), key=lambda index: (dates[index], alert_ids[index]))
    return tuple(alert_ids[index] for index in ranked)


def model_order(alert_ids: list[int] | tuple[int, ...], scores: list[float] | tuple[float, ...]) -> tuple[int, ...]:
    """Supervised order: higher Report score first, then lower AlertID."""

    if len(alert_ids) != len(scores):
        raise SynthamlError("Scores and alert identifiers have different lengths.")
    ranked = sorted(range(len(alert_ids)), key=lambda index: (-scores[index], alert_ids[index]))
    return tuple(alert_ids[index] for index in ranked)


def measure(
    order: tuple[int, ...],
    outcomes: dict[int, str],
    k: int,
    test_reports: int,
) -> CapacityMetrics:
    """Precision, recall, and missed Report alerts for the first K identifiers."""

    if k < 1 or k > len(order):
        raise SynthamlError("Review capacity K must be between 1 and the test alert count.")
    if test_reports < 1:
        raise SynthamlError("Recall is undefined when the test period has no Report outcomes.")
    identified = sum(1 for alert_id in order[:k] if outcomes[alert_id] == "Report")
    return CapacityMetrics(
        alerts_reviewed=k,
        reports_identified=identified,
        precision_at_k=identified / k,
        recall_at_k=identified / test_reports,
        reports_remaining=test_reports - identified,
    )


def compare_capacities(
    alert_ids: tuple[int, ...],
    dates: tuple[date, ...],
    scores: tuple[float, ...],
    outcomes: dict[int, str],
    test_reports: int,
) -> tuple[CapacityComparison, ...]:
    """Compare the two orders at every declared capacity."""

    chrono = chronological_order(alert_ids, dates)
    ranked = model_order(alert_ids, scores)
    comparisons: list[CapacityComparison] = []
    for numerator, denominator in CAPACITIES:
        k = review_capacity(len(alert_ids), numerator, denominator)
        baseline = measure(chrono, outcomes, k, test_reports)
        supervised = measure(ranked, outcomes, k, test_reports)
        comparisons.append(
            CapacityComparison(
                percent=numerator,
                alerts_reviewed=k,
                test_reports=test_reports,
                chronological=baseline,
                supervised=supervised,
                reports_identified_ml_minus_baseline=(
                    supervised.reports_identified - baseline.reports_identified
                ),
                precision_ml_minus_baseline=supervised.precision_at_k - baseline.precision_at_k,
                recall_ml_minus_baseline=supervised.recall_at_k - baseline.recall_at_k,
                reports_remaining_ml_minus_baseline=(
                    supervised.reports_remaining - baseline.reports_remaining
                ),
            )
        )
    return tuple(comparisons)

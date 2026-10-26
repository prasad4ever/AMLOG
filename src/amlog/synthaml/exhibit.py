"""Read the completed SynthAML evaluation artifact for display.

This module does not fit a model and does not open a database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.names import EXPERIMENT_ID, LIMITATIONS

SAMPLE_SIZE = 8


@dataclass(frozen=True)
class RankedResearchAlert:
    """One held-out alert taken from the saved score file."""

    alert_id: int
    score: float
    outcome: str
    model_position: int


@dataclass(frozen=True)
class CapacityExhibit:
    """One predeclared review capacity from the saved comparison."""

    percent: int
    alerts_reviewed: int
    date_reports: int
    supervised_reports: int
    date_precision: float
    supervised_precision: float
    date_recall: float
    supervised_recall: float
    date_remaining: int
    supervised_remaining: int
    reports_difference: int


@dataclass(frozen=True)
class ResearchExhibit:
    """The completed experiment, reduced to what a read-only page can show."""

    experiment_id: str
    model_name: str
    model_version: str
    algorithm_family: str
    sklearn_version: str
    random_state: int
    n_iter: int
    converged: bool
    alerts: int
    train_alerts: int
    test_alerts: int
    reports: int
    dismissals: int
    train_reports: int
    train_dismissals: int
    test_reports: int
    test_dismissals: int
    comparisons: tuple[CapacityExhibit, ...]
    sample: tuple[RankedResearchAlert, ...]
    limitations: tuple[str, ...]
    score_semantics: str


def load_exhibit(path: Path) -> ResearchExhibit | None:
    """Load a saved evaluation document. A missing file is an empty exhibit, not a new fit."""

    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("experiment_id") != EXPERIMENT_ID:
        raise SynthamlError("The evaluation artifact is not the completed SynthAML experiment.")
    counts = document["counts"]
    model = document["model"]
    comparisons = tuple(_capacity(item) for item in document["comparisons"])
    sample = _sample(document.get("test_predictions") or [])
    limitations = tuple(document.get("limitations") or LIMITATIONS)
    return ResearchExhibit(
        experiment_id=str(document["experiment_id"]),
        model_name=str(model["model_name"]),
        model_version=str(model["model_version"]),
        algorithm_family=str(model["algorithm_family"]),
        sklearn_version=str(model["sklearn_version"]),
        random_state=int(model["random_state"]),
        n_iter=int(model["n_iter"]),
        converged=bool(model["converged"]),
        alerts=int(counts["alerts"]),
        train_alerts=int(counts["train_alerts"]),
        test_alerts=int(counts["test_alerts"]),
        reports=int(counts["reports"]),
        dismissals=int(counts["dismissals"]),
        train_reports=int(counts["train_reports"]),
        train_dismissals=int(counts["train_alerts"]) - int(counts["train_reports"]),
        test_reports=int(counts["test_reports"]),
        test_dismissals=int(counts["test_alerts"]) - int(counts["test_reports"]),
        comparisons=comparisons,
        sample=sample,
        limitations=limitations,
        score_semantics=str(document["score_semantics"]),
    )


def _capacity(item: dict) -> CapacityExhibit:
    chronological = item["chronological"]
    supervised = item["supervised"]
    return CapacityExhibit(
        percent=int(item["percent"]),
        alerts_reviewed=int(item["alerts_reviewed"]),
        date_reports=int(chronological["reports_identified"]),
        supervised_reports=int(supervised["reports_identified"]),
        date_precision=float(chronological["precision_at_k"]),
        supervised_precision=float(supervised["precision_at_k"]),
        date_recall=float(chronological["recall_at_k"]),
        supervised_recall=float(supervised["recall_at_k"]),
        date_remaining=int(chronological["reports_remaining"]),
        supervised_remaining=int(supervised["reports_remaining"]),
        reports_difference=int(item["reports_identified_ml_minus_baseline"]),
    )


def _sample(rows: list[dict]) -> tuple[RankedResearchAlert, ...]:
    ranked = sorted(rows, key=lambda row: (int(row["model_position"]), int(row["alert_id"])))
    chosen = ranked[:SAMPLE_SIZE]
    return tuple(
        RankedResearchAlert(
            alert_id=int(row["alert_id"]),
            score=float(row["score"]),
            outcome=str(row["outcome"]),
            model_position=int(row["model_position"]),
        )
        for row in chosen
    )

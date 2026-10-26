"""Load the alert-linked investigation-outcome benchmark.

The command writes only to a disposable benchmark database. It replays the
existing R001 and R002 rules and stores alert_behavior snapshots. It does not
train a model and it does not report prediction accuracy.
"""

from __future__ import annotations

import argparse
import sys

from amlog.benchmark.store import BenchmarkCheckError, BenchmarkLoad, BenchmarkTargetError, load_benchmark
from amlog.config import load_settings

DEFAULT_DATABASE = "amlog_benchmark"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Link constructed benchmark cases to rule-generated alerts in a disposable database."
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the disposable benchmark database before loading.",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    try:
        loaded = load_benchmark(settings.database_url, args.database, reset=args.reset)
    except (BenchmarkTargetError, BenchmarkCheckError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_report(loaded))
    return 0


def format_report(loaded: BenchmarkLoad) -> str:
    """Describe the linked benchmark without a prediction metric."""

    assessment = loaded.assessment
    lines = [
        f"Benchmark database: {loaded.database_name}",
        f"Design: {loaded.design_id}",
        f"Phase 10A dossiers assessed: {loaded.phase10a.cases}",
        f"Phase 10A dossiers meeting an R001 or R002 threshold: "
        f"{loaded.phase10a.r001_threshold_met + loaded.phase10a.r002_threshold_met}",
        f"Histories specified: {assessment.case_count}",
        f"Linked to a rule-generated alert: {loaded.linked_cases}",
        f"Usable alert-level feature snapshots: {loaded.feature_snapshots}",
        f"Defensible constructed outcomes: {loaded.defensible_outcomes}",
        (
            "Fit population: "
            f"{assessment.fit_productive} productive_review, "
            f"{assessment.fit_non_productive} non_productive_review"
        ),
        (
            "Holdout population: "
            f"{assessment.holdout_productive} productive_review, "
            f"{assessment.holdout_non_productive} non_productive_review"
        ),
        *loaded.assessment.notes,
        "Checks passed: provenance, temporal cutoff, alert linkage, and feature linkage.",
        "No model was trained.",
        "Prediction accuracy was not computed.",
        (
            "These judgments are benchmark protocol results from post-alert support findings. "
            "They are not investigator findings and they are not operational training labels."
        ),
        (
            "The fit and holdout counts are below the 25-per-class floor used by the "
            "operational supervised gate. A controlled supervised experiment is not ready."
        ),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

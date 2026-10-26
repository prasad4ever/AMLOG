"""Score stored alerts with the experiment anomaly ranking.

The development database is refused. The command does not approve a model
and it does not calculate false-positive metrics.
"""

from __future__ import annotations

import argparse
import sys

from amlog.config import load_settings
from amlog.prioritize.errors import PriorityError
from amlog.prioritize.names import N_ESTIMATORS
from amlog.prioritize.store import PROTECTED_DATABASES, assert_priority_database, run_prioritization
from amlog.workload.load import replace_database

DEFAULT_DATABASE = "amlog_workload_integrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write experiment anomaly ranks for stored alerts.")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--estimators", type=int, default=N_ESTIMATORS)
    args = parser.parse_args(argv)
    try:
        assert_priority_database(args.database)
        settings = load_settings()
    except (PriorityError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.database in PROTECTED_DATABASES:
        print(f"{args.database} is not a scoring database.", file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    import psycopg

    url = replace_database(settings.database_url, args.database)
    with psycopg.connect(url, connect_timeout=5) as connection:
        try:
            result = run_prioritization(connection, n_estimators=args.estimators)
        except PriorityError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Alerts seen: {result.alerts_seen}.")
    print(f"Scores written: {result.scores_written}.")
    print(f"Scores already present: {result.already_present}.")
    print(f"Training alerts: {result.training_alerts}. Holdout alerts: {result.holdout_alerts}.")
    if result.holdout_rank_agreement is None:
        print("Holdout agreement with the baseline: not calculated.")
    else:
        print(f"Holdout rank agreement with the baseline: {result.holdout_rank_agreement:.6f}.")
    print(f"Seconds: {result.elapsed_seconds:.3f}.")
    print("Score use: experiment. The model is not approved for production.")
    print(
        "This is an anomaly rank, not a probability of financial crime, a productive investigation, or a false positive."
    )
    print("Supervised false-positive metrics were not calculated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

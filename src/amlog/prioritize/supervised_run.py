"""Check whether supervised false-positive training is allowed.

When the reviewed outcomes are not sufficient, the command stops. It does
not invent labels and it does not print precision, recall, or ROC-AUC.
"""

from __future__ import annotations

import argparse
import sys

from amlog.config import load_settings
from amlog.prioritize.errors import PriorityError, SupervisedTrainingBlocked
from amlog.prioritize.store import PROTECTED_DATABASES, assert_priority_database
from amlog.prioritize.supervised import assess_supervised_readiness, train_supervised
from amlog.workload.load import replace_database

DEFAULT_DATABASE = "amlog_workload_integrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a supervised alert model only when reviewed outcomes allow it.")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        assert_priority_database(args.database)
        settings = load_settings()
    except (PriorityError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.database in PROTECTED_DATABASES or settings.database_url is None:
        if settings.database_url is None:
            print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        else:
            print(f"{args.database} is not a scoring database.", file=sys.stderr)
        return 2
    import psycopg

    url = replace_database(settings.database_url, args.database)
    with psycopg.connect(url, connect_timeout=5) as connection:
        readiness = assess_supervised_readiness(connection)
        if readiness.blocked:
            print(readiness.message)
            print(f"Eligible productive outcomes: {readiness.eligible_productive}.")
            print(f"Eligible non-productive outcomes: {readiness.eligible_non_productive}.")
            print(f"Distinct reviewers: {readiness.distinct_reviewers}.")
            return 3
        try:
            train_supervised(connection)
        except SupervisedTrainingBlocked as exc:
            print(exc.message)
            return 3
    print("A supervised model version was registered with status registered.")
    print("It is not validated and it is not approved. No scores were written.")
    print("No precision, recall, or ROC-AUC was computed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Report whether a controlled review-efficiency comparison can be run.

The development database is refused. The command does not write labels,
scores, or cases, and it does not approve a model.
"""

from __future__ import annotations

import argparse
import sys

from amlog.config import load_settings
from amlog.evaluate.outcomes import assess_outcome_evidence, load_outcome_evidence
from amlog.prioritize.errors import PriorityError
from amlog.prioritize.store import PROTECTED_DATABASES, assert_priority_database
from amlog.workload.load import replace_database

DEFAULT_DATABASE = "amlog_workload_integrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether investigation-efficiency metrics can be calculated."
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        assert_priority_database(args.database)
        settings = load_settings()
    except (PriorityError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.database in PROTECTED_DATABASES:
        print(f"{args.database} is not an evaluation database.", file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    import psycopg

    url = replace_database(settings.database_url, args.database)
    with psycopg.connect(url, connect_timeout=5) as connection:
        evidence = load_outcome_evidence(connection)
        before = (
            evidence.label_rows,
            evidence.risk_scores,
            evidence.model_versions,
            evidence.investigation_cases,
        )
        evaluation = assess_outcome_evidence(evidence)
        after = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM amlog_label.alert_labels),
                (SELECT count(*) FROM amlog.risk_scores),
                (SELECT count(*) FROM amlog.model_versions),
                (SELECT count(*) FROM amlog.investigation_cases)
            """
        ).fetchone()
    if tuple(int(value) for value in after) != before:
        print("The evaluation changed stored rows. That is not allowed.", file=sys.stderr)
        return 2
    print(evaluation.message)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line entry for alert-behavior snapshots.

The default database is the integrated workload. The development database
is refused. The command does not train a model.
"""

from __future__ import annotations

import argparse
import sys

from amlog.config import load_settings
from amlog.features.store import PROTECTED_DATABASES, FeatureError, run_feature_snapshots
from amlog.workload.load import replace_database

DEFAULT_DATABASE = "amlog_workload_integrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Store point-in-time behavioral feature snapshots for existing alerts."
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    if args.database in PROTECTED_DATABASES:
        print(
            f"{args.database} is not a workload database. Feature snapshots were not written.",
            file=sys.stderr,
        )
        return 2
    try:
        settings = load_settings()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if settings.database_url is None:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    import psycopg

    try:
        with psycopg.connect(replace_database(settings.database_url, args.database), connect_timeout=5) as connection:
            report = run_feature_snapshots(connection)
    except FeatureError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_report(report)
    return 0


def _print_report(report) -> None:
    print(f"Database: {report.database_name}")
    print(f"Feature set: {report.feature_set_name} version {report.feature_set_version}")
    print(f"Alerts seen: {report.alerts_seen}")
    print(f"Snapshots recorded: {report.snapshots_recorded}")
    print(f"Snapshots already present: {report.snapshots_already_present}")
    print(f"Alerts without a full 30-day history: {report.alerts_without_full_30d_history}")
    print(f"Feature names in this run: {len(report.feature_names)}")
    print(f"Seconds: {report.elapsed_seconds:.3f}")
    if report.elapsed_seconds > 0:
        print(f"Alerts per second: {report.alerts_seen / report.elapsed_seconds:.1f}")
    print("Availability basis: event_timestamp. This is a historical reconstruction.")
    print("No model was trained. Scenario notes and labels were not read.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0) from None

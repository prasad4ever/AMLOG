"""Replay one versioned monitoring rule against a disposable workload database.

The development database named amlog is refused before any rule or alert row
is written. The default rule is R001. R002 is selected with --rule R002.
This command is a historical replay. It does not start live monitoring.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, urlunparse

from amlog.monitor.engine import (
    MonitoringError,
    assert_replay_database,
    run_r001_replay,
    run_r002_replay,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    database_name = "amlog_workload"
    rule_code = "R001"
    remaining = list(arguments)
    while remaining:
        if remaining[0] == "--database" and len(remaining) >= 2:
            database_name = remaining[1]
            remaining = remaining[2:]
            continue
        if remaining[0] == "--rule" and len(remaining) >= 2:
            rule_code = remaining[1]
            remaining = remaining[2:]
            continue
        print(
            "Usage: python -m amlog.monitor.run [--rule R001|R002] --database amlog_workload",
            file=sys.stderr,
        )
        return 2
    if rule_code not in {"R001", "R002"}:
        print("The rule must be R001 or R002.", file=sys.stderr)
        return 2
    try:
        assert_replay_database(database_name)
    except MonitoringError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    source = os.environ.get("AMLOG_DATABASE_URL", "").strip()
    if not source:
        print("AMLOG_DATABASE_URL is not set. No database was contacted.", file=sys.stderr)
        return 2
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. No database was contacted.", file=sys.stderr)
        return 2
    with psycopg.connect(replace_database(source, database_name)) as connection:
        if rule_code == "R002":
            result = run_r002_replay(connection)
        else:
            result = run_r001_replay(connection)
    print(f"Mode: {result.mode}")
    print(f"Database: {result.database_name}")
    print(f"Rule: {result.rule_code} version {result.version_number}")
    print(f"Transactions considered: {result.transactions_considered}")
    print(f"Alerts recorded this run: {result.alerts_recorded}")
    print(f"Alerts already present: {result.alerts_already_present}")
    print(f"Stored alerts: {result.stored_alerts}")
    print(f"Affected accounts: {result.affected_accounts}")
    print(f"Linked transactions: {result.linked_transactions}")
    print(f"Elapsed seconds: {result.elapsed_seconds:.3f}")
    return 0


def replace_database(database_url: str, database_name: str) -> str:
    parts = urlparse(database_url)
    return urlunparse(parts._replace(path=f"/{database_name}"))


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line for the isolated SynthAML research benchmark."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from amlog.synthaml.dataset import resolve_alert_file, resolve_transaction_file
from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.experiment import format_report, run_experiment


def default_artifact_dir() -> Path:
    """Local experiment output. The directory is gitignored with the rest of var/."""

    return Path(__file__).resolve().parents[3] / "var" / "synthaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train and evaluate one logistic regression on the published SynthAML "
            "research benchmark. This command does not open an AMLOG database."
        )
    )
    parser.add_argument("--alerts", type=Path)
    parser.add_argument("--transactions", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        alerts = resolve_alert_file(args.alerts)
        transactions = resolve_transaction_file(args.transactions)
        result = run_experiment(
            alerts,
            transactions,
            args.artifact_dir or default_artifact_dir(),
            published_checks=True,
            progress=_progress,
        )
    except SynthamlError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_report(result))
    return 0


def _progress(rows: int) -> None:
    print(f"transactions_read {rows}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

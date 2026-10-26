"""Locate the published SynthAML CSV files outside the git repository."""

from __future__ import annotations

import hashlib
import os
import urllib.request
from datetime import date
from pathlib import Path

from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.names import (
    ALERT_URL,
    PUBLISHED_ALERT_BYTES,
    PUBLISHED_TRANSACTION_BYTES,
    TRANSACTION_URL,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def default_raw_directory() -> Path:
    """Local cache for the two CSV files. This directory is not the repository."""

    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Temp" / "synthaml-assess"
    return Path.home() / ".cache" / "synthaml-assess"


def assert_outside_repository(path: Path) -> None:
    """Refuse a raw dataset path inside the project tree, including gitignored folders."""

    resolved = path.resolve()
    if resolved == _REPO_ROOT or _REPO_ROOT in resolved.parents:
        raise SynthamlError(
            "Raw SynthAML files stay outside the repository. "
            f"{resolved} is inside the project tree."
        )


def file_sha256(path: Path) -> str:
    """Hash a file in blocks."""

    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def load_alert_dates(path: Path) -> dict[int, date]:
    """Read AlertID and Date. The outcome column is not returned."""

    dates: dict[int, date] = {}
    _read_alert_file(path, dates=dates, outcomes=None)
    return dates


def load_outcomes(path: Path) -> dict[int, str]:
    """Read AlertID and Outcome. Dates are not returned."""

    outcomes: dict[int, str] = {}
    _read_alert_file(path, dates=None, outcomes=outcomes)
    return outcomes


def resolve_alert_file(explicit: Path | None = None) -> Path:
    """Use a local alert file of the published size, or download it outside the repository."""

    return _resolve(
        explicit,
        "SYNTHAML_ALERTS",
        "synthetic_alerts.csv",
        ALERT_URL,
        PUBLISHED_ALERT_BYTES,
    )


def resolve_transaction_file(explicit: Path | None = None) -> Path:
    """Use a local transaction file of the published size, or download it outside the repository."""

    return _resolve(
        explicit,
        "SYNTHAML_TRANSACTIONS",
        "synthetic_transactions.csv",
        TRANSACTION_URL,
        PUBLISHED_TRANSACTION_BYTES,
    )


def _resolve(explicit: Path | None, env_name: str, filename: str, url: str, expected_bytes: int) -> Path:
    if explicit is not None:
        return _require_size(explicit, expected_bytes)
    env_value = os.environ.get(env_name)
    if env_value:
        return _require_size(Path(env_value), expected_bytes)
    destination = default_raw_directory() / filename
    assert_outside_repository(destination)
    if destination.is_file() and destination.stat().st_size == expected_bytes:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    urllib.request.urlretrieve(url, partial)
    size = partial.stat().st_size
    if size != expected_bytes:
        partial.unlink(missing_ok=True)
        raise SynthamlError(
            f"The download for {filename} is {size} bytes. "
            f"The published file is {expected_bytes} bytes."
        )
    partial.replace(destination)
    return destination


def _require_size(path: Path, expected_bytes: int) -> Path:
    assert_outside_repository(path)
    if not path.is_file():
        raise SynthamlError(f"The dataset file does not exist: {path}")
    size = path.stat().st_size
    if size != expected_bytes:
        raise SynthamlError(
            f"{path.name} is {size} bytes. The published file is {expected_bytes} bytes."
        )
    return path


def _read_alert_file(
    path: Path,
    dates: dict[int, date] | None,
    outcomes: dict[int, str] | None,
) -> None:
    with path.open("rb") as handle:
        header_line = handle.readline()
        if not header_line:
            raise SynthamlError("The alert file is empty.")
        header = header_line.lstrip(b"\xef\xbb\xbf").decode("utf-8").strip().split(",")
        if header != ["AlertID", "Date", "Outcome"]:
            raise SynthamlError("The alert columns are not the published schema.")
        for line_number, line in enumerate(handle, start=2):
            if not line.strip():
                continue
            parts = line.decode("utf-8").rstrip("\r\n").split(",")
            if len(parts) != 3:
                raise SynthamlError(f"Alert row {line_number} does not have three fields.")
            alert_id = int(parts[0])
            if dates is not None:
                if alert_id in dates:
                    raise SynthamlError(f"AlertID {alert_id} is repeated.")
                if len(parts[1]) != 10:
                    raise SynthamlError(f"AlertID {alert_id} does not have a calendar date.")
                dates[alert_id] = date.fromisoformat(parts[1])
            if outcomes is not None:
                if alert_id in outcomes:
                    raise SynthamlError(f"AlertID {alert_id} is repeated.")
                if parts[2] not in ("Report", "Dismiss"):
                    raise SynthamlError(f"AlertID {alert_id} has an outcome outside Report and Dismiss.")
                outcomes[alert_id] = parts[2]
    target = dates if dates is not None else outcomes
    if not target:
        raise SynthamlError("The alert file has no data rows.")

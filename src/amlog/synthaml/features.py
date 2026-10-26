"""Published 56-feature histories for SynthAML alerts.

Each alert contributes seven statistics for every combination of the four
transaction types and two entry categories. The builder reads transaction
rows and alert dates. It has no parameter for an investigation outcome.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np

from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.names import (
    COUNT_OFFSET,
    ENTRIES,
    FEATURE_NAMES,
    MISSING_GROUP_SENTINEL,
    STATISTICS,
    TYPES,
)

_GROUP_COUNT = len(ENTRIES) * len(TYPES)
_STAT_COUNT = len(STATISTICS)


class _UnsortedTransactions(Exception):
    """Alert identifiers are not contiguous, so the file needs a compact sort."""


@dataclass(frozen=True)
class FeatureBuild:
    """Unscaled feature matrix. Rows follow ascending AlertID. Outcomes are absent."""

    alert_ids: tuple[int, ...]
    alert_dates: tuple[date, ...]
    matrix: np.ndarray
    transaction_rows: int
    rows_used: int
    future_rows: int
    older_than_365_rows: int
    alerts_without_transactions: int
    transaction_sha256: str
    grouped_read: bool

    @property
    def fingerprint(self) -> str:
        hasher = hashlib.sha256()
        hasher.update("\n".join(FEATURE_NAMES).encode("utf-8"))
        hasher.update(",".join(str(alert_id) for alert_id in self.alert_ids).encode("utf-8"))
        hasher.update(np.ascontiguousarray(self.matrix, dtype=np.float64).tobytes())
        return hasher.hexdigest()


def build_feature_matrix(
    alert_dates: dict[int, date],
    transaction_path: Path,
    progress: Callable[[int], None] | None = None,
) -> FeatureBuild:
    """Aggregate the transaction file without holding every row as a Python object."""

    if not alert_dates:
        raise SynthamlError("The alert file has no dates.")
    try:
        return _build_grouped(alert_dates, transaction_path, progress)
    except _UnsortedTransactions:
        return _build_sorted(alert_dates, transaction_path, progress)


def group_index(entry: str, transaction_type: str) -> int:
    """Map a published entry and type onto one of the eight feature groups."""

    if entry not in ENTRIES or transaction_type not in TYPES:
        raise SynthamlError(
            f"Transaction entry {entry!r} and type {transaction_type!r} "
            "are outside the published categories."
        )
    return ENTRIES.index(entry) * len(TYPES) + TYPES.index(transaction_type)


def _empty_matrix(alert_count: int) -> np.ndarray:
    matrix = np.full((alert_count, len(FEATURE_NAMES)), MISSING_GROUP_SENTINEL, dtype=np.float64)
    for group in range(_GROUP_COUNT):
        matrix[:, group * _STAT_COUNT + COUNT_OFFSET] = 0.0
    return matrix


def _statistics(values: np.ndarray) -> np.ndarray:
    """Seven published statistics. An empty group keeps the sentinel and a zero count."""

    out = np.full(_STAT_COUNT, MISSING_GROUP_SENTINEL, dtype=np.float64)
    count = int(values.size)
    out[COUNT_OFFSET] = float(count)
    if count == 0:
        return out
    out[0] = float(values.min())
    out[1] = float(values.mean())
    out[2] = float(np.median(values))
    out[3] = float(values.max())
    out[4] = float(values.std(ddof=0))
    out[6] = float(values.sum())
    return out


def _write_group(matrix: np.ndarray, row: int, group: int, values: np.ndarray) -> None:
    start = group * _STAT_COUNT
    matrix[row, start : start + _STAT_COUNT] = _statistics(values)


def _prepare(alert_dates: dict[int, date]) -> tuple[tuple[int, ...], dict[int, int], np.ndarray]:
    ordered = tuple(sorted(alert_dates))
    index = {alert_id: position for position, alert_id in enumerate(ordered)}
    return ordered, index, _empty_matrix(len(ordered))


def _header(line: bytes) -> list[str]:
    text = line.lstrip(b"\xef\xbb\xbf").decode("utf-8").strip()
    return text.split(",")


def _build_grouped(
    alert_dates: dict[int, date],
    transaction_path: Path,
    progress: Callable[[int], None] | None,
) -> FeatureBuild:
    ordered, index, matrix = _prepare(alert_dates)
    hasher = hashlib.sha256()
    seen: set[int] = set()
    finalized: set[int] = set()
    buckets: list[list[float]] = [[] for _ in range(_GROUP_COUNT)]
    current_id: int | None = None
    rows = 0
    future_rows = 0
    older_rows = 0
    used = 0

    def flush() -> None:
        nonlocal current_id
        if current_id is None:
            return
        row = index[current_id]
        for group, values in enumerate(buckets):
            _write_group(matrix, row, group, np.asarray(values, dtype=np.float64))
            values.clear()
        finalized.add(current_id)
        seen.add(current_id)
        current_id = None

    with transaction_path.open("rb") as handle:
        header_line = handle.readline()
        if not header_line:
            raise SynthamlError("The transaction file is empty.")
        hasher.update(header_line)
        header = _header(header_line)
        if header != ["AlertID", "Timestamp", "Entry", "Type", "Size"]:
            raise SynthamlError("The transaction columns are not the published schema.")
        for line in handle:
            hasher.update(line)
            if not line.strip():
                continue
            rows += 1
            parts = line.decode("utf-8").rstrip("\r\n").split(",")
            if len(parts) != 5:
                raise SynthamlError(f"Transaction row {rows} does not have five fields.")
            alert_id = int(parts[0])
            if alert_id not in index:
                raise SynthamlError(f"AlertID {alert_id} is in the transactions and not in the alert file.")
            if alert_id != current_id:
                if alert_id in finalized:
                    raise _UnsortedTransactions()
                flush()
                current_id = alert_id
            timestamp = datetime.fromisoformat(parts[1])
            alert_day = alert_dates[alert_id]
            if timestamp.date() > alert_day:
                future_rows += 1
                continue
            if (alert_day - timestamp.date()).days > 365:
                older_rows += 1
            buckets[group_index(parts[2], parts[3])].append(float(parts[4]))
            used += 1
            if progress is not None and rows % 2_000_000 == 0:
                progress(rows)
        flush()
    if rows == 0:
        raise SynthamlError("The transaction file has no data rows.")
    return FeatureBuild(
        alert_ids=ordered,
        alert_dates=tuple(alert_dates[alert_id] for alert_id in ordered),
        matrix=matrix,
        transaction_rows=rows,
        rows_used=used,
        future_rows=future_rows,
        older_than_365_rows=older_rows,
        alerts_without_transactions=len(ordered) - len(seen),
        transaction_sha256=hasher.hexdigest(),
        grouped_read=True,
    )


def _build_sorted(
    alert_dates: dict[int, date],
    transaction_path: Path,
    progress: Callable[[int], None] | None,
) -> FeatureBuild:
    ordered, index, matrix = _prepare(alert_dates)
    hasher = hashlib.sha256()
    capacity = 1_000_000
    alert_ids = np.empty(capacity, dtype=np.int64)
    groups = np.empty(capacity, dtype=np.uint8)
    sizes = np.empty(capacity, dtype=np.float64)
    stored = 0
    rows = 0
    future_rows = 0
    older_rows = 0

    def grow() -> None:
        nonlocal capacity, alert_ids, groups, sizes
        new_capacity = capacity * 2
        grown_ids = np.empty(new_capacity, dtype=np.int64)
        grown_groups = np.empty(new_capacity, dtype=np.uint8)
        grown_sizes = np.empty(new_capacity, dtype=np.float64)
        grown_ids[:capacity] = alert_ids
        grown_groups[:capacity] = groups
        grown_sizes[:capacity] = sizes
        capacity = new_capacity
        alert_ids = grown_ids
        groups = grown_groups
        sizes = grown_sizes

    with transaction_path.open("rb") as handle:
        header_line = handle.readline()
        if not header_line:
            raise SynthamlError("The transaction file is empty.")
        hasher.update(header_line)
        if _header(header_line) != ["AlertID", "Timestamp", "Entry", "Type", "Size"]:
            raise SynthamlError("The transaction columns are not the published schema.")
        for line in handle:
            hasher.update(line)
            if not line.strip():
                continue
            rows += 1
            parts = line.decode("utf-8").rstrip("\r\n").split(",")
            if len(parts) != 5:
                raise SynthamlError(f"Transaction row {rows} does not have five fields.")
            alert_id = int(parts[0])
            if alert_id not in index:
                raise SynthamlError(f"AlertID {alert_id} is in the transactions and not in the alert file.")
            timestamp = datetime.fromisoformat(parts[1])
            alert_day = alert_dates[alert_id]
            if timestamp.date() > alert_day:
                future_rows += 1
                continue
            if (alert_day - timestamp.date()).days > 365:
                older_rows += 1
            if stored == capacity:
                grow()
            alert_ids[stored] = alert_id
            groups[stored] = group_index(parts[2], parts[3])
            sizes[stored] = float(parts[4])
            stored += 1
            if progress is not None and rows % 2_000_000 == 0:
                progress(rows)
    if rows == 0:
        raise SynthamlError("The transaction file has no data rows.")
    order = np.argsort(alert_ids[:stored], kind="mergesort")
    seen: set[int] = set()
    cursor = 0
    while cursor < stored:
        alert_id = int(alert_ids[order[cursor]])
        end = cursor + 1
        while end < stored and int(alert_ids[order[end]]) == alert_id:
            end += 1
        collected: list[list[float]] = [[] for _ in range(_GROUP_COUNT)]
        for position in order[cursor:end]:
            collected[int(groups[position])].append(float(sizes[position]))
        row = index[alert_id]
        for group, values in enumerate(collected):
            _write_group(matrix, row, group, np.asarray(values, dtype=np.float64))
        seen.add(alert_id)
        cursor = end
    return FeatureBuild(
        alert_ids=ordered,
        alert_dates=tuple(alert_dates[alert_id] for alert_id in ordered),
        matrix=matrix,
        transaction_rows=rows,
        rows_used=stored,
        future_rows=future_rows,
        older_than_365_rows=older_rows,
        alerts_without_transactions=len(ordered) - len(seen),
        transaction_sha256=hasher.hexdigest(),
        grouped_read=False,
    )

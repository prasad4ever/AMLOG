"""Build a numeric matrix from alert feature snapshots.

Statistics are fit on the training accounts only. An amount is scaled inside
its own currency. A currency with too little training history is left missing
rather than compared with another currency. Identifiers, rule codes, labels,
and scenario notes are not columns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Sequence
from uuid import UUID

from amlog.prioritize.errors import PriorityError
from amlog.prioritize.names import MIN_CURRENCY_OBSERVATIONS, RANDOM_SEED, TRAINING_FRACTION

# Present on the snapshot, and deliberately not model columns.
# amount_currency is read only to choose a within-currency scale.
EXCLUDED_INPUTS = frozenset(
    {
        "evaluation_event_time",
        "availability_basis",
        "amount_currency",
        "rule_code",
        "rule_version",
        "alert_id",
        "account_id",
        "customer_id",
        "transaction_id",
        "label_value",
        "disposition",
        "scenario_code",
    }
)

AMOUNT_FEATURES = (
    "same_currency_debit_sum_24h",
    "same_currency_debit_sum_30d",
    "same_currency_credit_sum_24h",
    "same_currency_debit_max_24h",
    "same_currency_debit_mean_30d",
    "triggering_amount",
)

BEHAVIOR_FEATURES = (
    "evidence_count",
    "account_opened_at_known",
    "history_covers_1h",
    "history_covers_24h",
    "history_covers_7d",
    "history_covers_30d",
    "payment_count_1h",
    "payment_count_24h",
    "payment_count_7d",
    "payment_count_30d",
    "debit_count_1h",
    "debit_count_24h",
    "debit_count_7d",
    "debit_count_30d",
    "credit_count_1h",
    "credit_count_24h",
    "credit_count_7d",
    "credit_count_30d",
    "same_currency_count_1h",
    "same_currency_count_24h",
    "same_currency_count_7d",
    "same_currency_count_30d",
    "other_currency_count_24h",
    "other_currency_count_30d",
    "distinct_currency_count_30d",
    "distinct_counterparty_count_24h",
    "distinct_counterparty_count_30d",
    "distinct_channel_count_7d",
    "card_count_24h",
    "wire_count_24h",
    "ach_count_24h",
    "wallet_count_24h",
    "account_age_seconds",
    "alert_window_seconds",
    "seconds_since_previous_payment",
    "debit_hourly_rate_1h_vs_prior_23h",
    "debit_daily_rate_24h_vs_prior_6d",
    "new_counterparty_count_24h",
    "triggering_counterparty_prior_count_30d",
)

MODEL_FEATURES = BEHAVIOR_FEATURES + AMOUNT_FEATURES


@dataclass(frozen=True)
class AlertSample:
    """One alert's latest snapshot, limited to values the model may see."""

    alert_id: UUID
    account_id: UUID
    currency: str | None
    values: Mapping[str, float | None]
    feature_computation_id: UUID | None = None
    fingerprint: str | None = None
    feature_set_name: str | None = None
    feature_set_version: str | None = None


@dataclass(frozen=True)
class PreparedMatrix:
    """Training-fitted columns and one numeric row per input sample."""

    columns: tuple[str, ...]
    rows: tuple[tuple[float, ...], ...]
    training_mask: tuple[bool, ...]
    medians: dict[str, float]
    imputation_medians: dict[str, float]
    scales: dict[tuple[str, str], tuple[float, float]]
    dropped_constant: tuple[str, ...]


def account_in_training(account_id: UUID, *, fraction: float = TRAINING_FRACTION, seed: int = RANDOM_SEED) -> bool:
    """Keep every alert for one account on the same side of the split."""

    if fraction >= 1:
        return True
    if fraction <= 0:
        return False
    digest = hashlib.sha256(f"{seed}:{account_id}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 1000
    return bucket < int(round(fraction * 1000))


def prepare(
    samples: Sequence[AlertSample],
    *,
    fraction: float = TRAINING_FRACTION,
    seed: int = RANDOM_SEED,
) -> PreparedMatrix:
    """Fit imputation and currency scales on training accounts, then transform every row."""

    if len(samples) < 2:
        raise PriorityError("At least two alerts with feature snapshots are required.")
    training_mask = tuple(
        account_in_training(sample.account_id, fraction=fraction, seed=seed) for sample in samples
    )
    training = [sample for sample, selected in zip(samples, training_mask) if selected]
    if len(training) < 2:
        raise PriorityError("The training split has fewer than two alerts.")
    scales = _currency_scales(training)
    medians, usable, indicators = _training_medians(training, scales)
    if not usable:
        raise PriorityError("No usable feature remained after the training-only fit.")
    wide_columns = _wide_columns(usable, indicators)
    training_rows = tuple(_dense_row(sample, scales, medians, wide_columns) for sample in training)
    kept, dropped = _varying_columns(wide_columns, training_rows)
    if not kept:
        raise PriorityError("Every training column was constant.")
    columns = tuple(wide_columns[index] for index in kept)
    rows = tuple(
        tuple(_dense_row(sample, scales, medians, wide_columns)[index] for index in kept)
        for sample in samples
    )
    return PreparedMatrix(
        columns=columns,
        rows=rows,
        training_mask=training_mask,
        medians={name: medians[name] for name in usable if name in columns},
        imputation_medians=dict(medians),
        scales=scales,
        dropped_constant=dropped,
    )


def _currency_scales(training: Sequence[AlertSample]) -> dict[tuple[str, str], tuple[float, float]]:
    grouped: dict[tuple[str, str], list[float]] = {}
    for sample in training:
        if not sample.currency:
            continue
        for name in AMOUNT_FEATURES:
            raw = sample.values.get(name)
            if raw is None:
                continue
            grouped.setdefault((name, sample.currency), []).append(float(raw))
    scales: dict[tuple[str, str], tuple[float, float]] = {}
    for key, values in grouped.items():
        if len(values) < MIN_CURRENCY_OBSERVATIONS:
            continue
        center = _median(values)
        spread = _median([abs(value - center) for value in values])
        if spread <= 0:
            continue
        scales[key] = (center, spread)
    return scales


def _training_medians(
    training: Sequence[AlertSample],
    scales: Mapping[tuple[str, str], tuple[float, float]],
) -> tuple[dict[str, float], tuple[str, ...], frozenset[str]]:
    medians: dict[str, float] = {}
    usable: list[str] = []
    indicators: list[str] = []
    for name in MODEL_FEATURES:
        observed = [value for sample in training if (value := _project(sample, name, scales)) is not None]
        if not observed:
            continue
        medians[name] = _median(observed)
        usable.append(name)
        if any(_project(sample, name, scales) is None for sample in training):
            indicators.append(name)
    return medians, tuple(usable), frozenset(indicators)


def _wide_columns(usable: Sequence[str], indicators: frozenset[str]) -> tuple[str, ...]:
    columns: list[str] = []
    for name in usable:
        columns.append(name)
        if name in indicators:
            columns.append(_missing_name(name))
    return tuple(columns)


def _varying_columns(
    columns: Sequence[str], rows: Sequence[Sequence[float]]
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    kept: list[int] = []
    dropped: list[str] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows]
        if min(values) == max(values):
            dropped.append(name)
        else:
            kept.append(index)
    return tuple(kept), tuple(dropped)


def _dense_row(
    sample: AlertSample,
    scales: Mapping[tuple[str, str], tuple[float, float]],
    medians: Mapping[str, float],
    columns: Sequence[str],
) -> tuple[float, ...]:
    projected = {name: _project(sample, name, scales) for name in medians}
    values: list[float] = []
    for column in columns:
        if column.endswith("__missing"):
            source = column[: -len("__missing")]
            values.append(1.0 if projected.get(source) is None else 0.0)
            continue
        observed = projected.get(column)
        values.append(medians[column] if observed is None else observed)
    return tuple(values)


def _project(
    sample: AlertSample,
    name: str,
    scales: Mapping[tuple[str, str], tuple[float, float]],
) -> float | None:
    if name in EXCLUDED_INPUTS or name not in MODEL_FEATURES:
        return None
    raw = sample.values.get(name)
    if raw is None:
        return None
    if name in AMOUNT_FEATURES:
        if not sample.currency:
            return None
        scale = scales.get((name, sample.currency))
        if scale is None:
            return None
        center, spread = scale
        return (float(raw) - center) / spread
    return float(raw)


def _missing_name(name: str) -> str:
    return f"{name}__missing"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0

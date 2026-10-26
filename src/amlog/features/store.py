"""Store alert-behavior snapshots in the existing feature tables.

A repeated run with the same alert and the same input fingerprint inserts
nothing. A changed input fingerprint inserts another snapshot. Older
snapshots stay. Alert rows, rule rows, and transactions are not updated.

The read path selects operational tables only. It does not read
``amlog_scenario`` or ``amlog_label``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from amlog.features.compute import (
    FEATURE_SET_NAME,
    FEATURE_SET_VERSION,
    WINDOW_30D,
    AlertView,
    FeatureSnapshot,
    FeatureValue,
    PaymentView,
    compute_snapshot,
)

PROTECTED_DATABASES = frozenset({"amlog", "postgres", "template0", "template1"})

_ALERT_QUERY = """
SELECT
    alerts.alert_id,
    alerts.account_id,
    alerts.customer_id,
    alerts.triggered_at,
    alerts.window_started_at,
    alerts.window_ended_at,
    alerts.recorded_rule_code,
    alerts.recorded_rule_version,
    accounts.opened_at,
    btrim(accounts.currency_code),
    COALESCE(evidence.evidence_count, 0),
    evidence.triggering_transaction_id
FROM amlog.alerts AS alerts
JOIN amlog.accounts AS accounts ON accounts.account_id = alerts.account_id
LEFT JOIN (
    SELECT
        alert_id,
        count(*)::integer AS evidence_count,
        (array_agg(transaction_id) FILTER (WHERE relationship_role = 'triggering'))[1]
            AS triggering_transaction_id
    FROM amlog.alert_transactions
    GROUP BY alert_id
) AS evidence ON evidence.alert_id = alerts.alert_id
WHERE alerts.account_id IS NOT NULL
ORDER BY alerts.account_id, alerts.triggered_at, alerts.alert_id
"""

_PAYMENT_QUERY = """
SELECT
    transaction_id,
    account_id,
    event_timestamp,
    direction,
    amount,
    btrim(currency_code),
    btrim(payment_channel),
    counterparty_reference
FROM amlog.transactions
WHERE account_id = %s
  AND event_timestamp <= %s
  AND event_timestamp >= %s
ORDER BY event_timestamp, transaction_id
"""


class FeatureError(RuntimeError):
    """A feature snapshot could not be calculated or stored."""


@dataclass(frozen=True)
class FeatureRun:
    """What one pass stored. This is not a model score and not a detection rate."""

    database_name: str
    feature_set_name: str
    feature_set_version: str
    alerts_seen: int
    snapshots_recorded: int
    snapshots_already_present: int
    alerts_without_full_30d_history: int
    feature_names: tuple[str, ...]
    elapsed_seconds: float


def assert_feature_database(database_name: str) -> None:
    """Refuse the development database and PostgreSQL's own databases."""

    if database_name in PROTECTED_DATABASES:
        raise FeatureError(
            f"{database_name} is not a workload database. Feature snapshots were not written."
        )


def run_feature_snapshots(connection: Any) -> FeatureRun:
    """Calculate and store one snapshot per alert that does not already have this input."""

    started = time.perf_counter()
    database_name = _database_name(connection)
    assert_feature_database(database_name)
    alerts = _load_alerts(connection)
    recorded = 0
    already = 0
    short_history = 0
    names: set[str] = set()
    for account_id, group in _by_account(alerts).items():
        earliest = min(alert.triggered_at for alert in group)
        latest = max(alert.triggered_at for alert in group)
        payments = _load_payments(connection, account_id, earliest - WINDOW_30D, latest)
        for alert in group:
            snapshot = compute_snapshot(alert, payments)
            names.update(value.name for value in snapshot.values)
            if _history_is_short(snapshot):
                short_history += 1
            if _snapshot_exists(connection, alert.alert_id, snapshot.input_fingerprint):
                already += 1
                continue
            _store_snapshot(connection, alert, snapshot)
            recorded += 1
    return FeatureRun(
        database_name=database_name,
        feature_set_name=FEATURE_SET_NAME,
        feature_set_version=FEATURE_SET_VERSION,
        alerts_seen=len(alerts),
        snapshots_recorded=recorded,
        snapshots_already_present=already,
        alerts_without_full_30d_history=short_history,
        feature_names=tuple(sorted(names)),
        elapsed_seconds=time.perf_counter() - started,
    )


def _database_name(connection: Any) -> str:
    row = connection.execute("SELECT current_database()").fetchone()
    return str(row[0])


def _load_alerts(connection: Any) -> tuple[AlertView, ...]:
    rows = connection.execute(_ALERT_QUERY).fetchall()
    return tuple(
        AlertView(
            alert_id=row[0],
            account_id=row[1],
            customer_id=row[2],
            triggered_at=row[3],
            window_started_at=row[4],
            window_ended_at=row[5],
            rule_code=row[6],
            rule_version=int(row[7]),
            account_opened_at=row[8],
            account_currency=row[9],
            evidence_count=int(row[10]),
            triggering_transaction_id=row[11],
        )
        for row in rows
    )


def _by_account(alerts: tuple[AlertView, ...]) -> dict[UUID, list[AlertView]]:
    grouped: dict[UUID, list[AlertView]] = {}
    for alert in alerts:
        grouped.setdefault(alert.account_id, []).append(alert)
    return grouped


def _load_payments(connection: Any, account_id: UUID, start: datetime, end: datetime) -> tuple[PaymentView, ...]:
    rows = connection.execute(_PAYMENT_QUERY, (account_id, end, start)).fetchall()
    return tuple(
        PaymentView(
            transaction_id=row[0],
            account_id=row[1],
            event_time=row[2],
            direction=row[3],
            amount=row[4] if isinstance(row[4], Decimal) else Decimal(row[4]),
            currency_code=row[5],
            payment_channel=row[6],
            counterparty_reference=row[7],
        )
        for row in rows
    )


def _history_is_short(snapshot: FeatureSnapshot) -> bool:
    for value in snapshot.values:
        if value.name == "history_covers_30d":
            return value.boolean is False
    return True


def _snapshot_exists(connection: Any, alert_id: UUID, fingerprint: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM amlog.feature_computations
        WHERE alert_id = %s
          AND feature_set_name = %s
          AND feature_set_version = %s
          AND input_fingerprint = %s
        LIMIT 1
        """,
        (alert_id, FEATURE_SET_NAME, FEATURE_SET_VERSION, fingerprint),
    ).fetchone()
    return row is not None


def _store_snapshot(connection: Any, alert: AlertView, snapshot: FeatureSnapshot) -> None:
    from psycopg.types.json import Json

    computed_at = datetime.now(timezone.utc)
    with connection.transaction():
        if _snapshot_exists(connection, alert.alert_id, snapshot.input_fingerprint):
            return
        computation_id = connection.execute(
            """
            INSERT INTO amlog.feature_computations (
                alert_id,
                feature_set_name,
                feature_set_version,
                input_fingerprint,
                computed_at
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING feature_computation_id
            """,
            (
                alert.alert_id,
                FEATURE_SET_NAME,
                FEATURE_SET_VERSION,
                snapshot.input_fingerprint,
                computed_at,
            ),
        ).fetchone()[0]
        cursor = connection.cursor()
        cursor.executemany(
            """
            INSERT INTO amlog.alert_features (
                feature_computation_id,
                alert_id,
                feature_name,
                value_type,
                value_numeric,
                value_text,
                value_boolean,
                value_timestamp
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [_row(computation_id, alert.alert_id, value) for value in snapshot.values],
        )
        connection.execute(
            """
            SELECT amlog.record_audit_event(
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::text,
                %s::uuid,
                NULL::uuid,
                %s::text,
                NULL::jsonb,
                %s::jsonb,
                %s::jsonb
            )
            """,
            (
                "system",
                "amlog.features",
                "features_computed",
                "amlog",
                "feature_computations",
                computation_id,
                snapshot.input_fingerprint,
                Json(
                    {
                        "feature_set_name": FEATURE_SET_NAME,
                        "feature_set_version": FEATURE_SET_VERSION,
                        "feature_count": len(snapshot.values),
                        "availability_basis": "event_timestamp",
                    }
                ),
                Json({"alert_id": str(alert.alert_id), "rule_code": alert.rule_code}),
            ),
        )


def _row(computation_id: UUID, alert_id: UUID, value: FeatureValue) -> tuple[Any, ...]:
    return (
        computation_id,
        alert_id,
        value.name,
        value.value_type,
        value.numeric,
        value.text,
        value.boolean,
        value.timestamp,
    )

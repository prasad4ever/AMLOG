"""Apply one active monitoring-rule version to transactions already stored.

The engine is an ordinary Python call. A later queue consumer can call the
same entry point. This module does not talk to AWS, and it does not read
scenario notes or evaluation labels.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from amlog.monitor.turnover import (
    R002_AUTHORED_BY,
    R002_CODE,
    R002_DESCRIPTION,
    R002_EFFECTIVE_FROM,
    R002_NAME,
    R002_PARAMETERS,
    R002_SEVERITY,
    R002_VERSION,
    TurnoverEpisode,
    TurnoverParameters,
    TurnoverTransaction,
    find_turnover_episodes,
    parse_turnover_parameters,
)
from amlog.monitor.velocity import (
    R001_AUTHORED_BY,
    R001_CODE,
    R001_DESCRIPTION,
    R001_EFFECTIVE_FROM,
    R001_NAME,
    R001_PARAMETERS,
    R001_SEVERITY,
    R001_VERSION,
    ObservedTransaction,
    VelocityBurst,
    VelocityParameters,
    find_velocity_bursts,
    parse_velocity_parameters,
)

RETROSPECTIVE_REPLAY = "retrospective_replay"
PROTECTED_DATABASES = frozenset({"amlog", "postgres", "template0", "template1"})

# Operational transactions only. Scenario notes and labels are not inputs.
TRANSACTION_QUERY = """
SELECT
    transaction_id,
    account_id,
    customer_id,
    direction,
    event_timestamp
FROM amlog.transactions
WHERE direction = %s
  AND event_timestamp >= %s
  AND (%s::timestamptz IS NULL OR event_timestamp < %s)
ORDER BY account_id, event_timestamp, transaction_id
"""


class MonitoringError(RuntimeError):
    """The rule could not be registered or applied."""


@dataclass(frozen=True)
class RuleVersion:
    monitoring_rule_id: UUID
    rule_code: str
    version_number: int
    severity: str
    effective_from: datetime
    effective_to: datetime | None
    parameters: VelocityParameters


@dataclass(frozen=True)
class MonitoringRun:
    """What one retrospective replay stored. This is not a live-monitoring result."""

    mode: str
    database_name: str
    monitoring_rule_id: UUID
    rule_code: str
    version_number: int
    transactions_considered: int
    alerts_recorded: int
    alerts_already_present: int
    stored_alerts: int
    affected_accounts: int
    linked_transactions: int
    elapsed_seconds: float


def assert_replay_database(database_name: str) -> None:
    """Refuse the development database and the PostgreSQL maintenance databases."""

    if database_name in PROTECTED_DATABASES:
        raise MonitoringError(
            f"{database_name} is not a workload database. "
            "Historical replay was not started."
        )


def register_r001(connection: Any) -> RuleVersion:
    """Insert R001 version 1 when it is absent, then return the active row.

    An existing version with different parameters is left unchanged and refused.
    A published version is immutable, so a new threshold must be a new version.
    """

    _guard_connection(connection)
    existing = _load_rule(connection, R001_CODE, R001_VERSION)
    if existing is None:
        _insert_r001(connection)
        existing = _load_rule(connection, R001_CODE, R001_VERSION)
    if existing is None:
        raise MonitoringError("R001 version 1 could not be read after registration")
    if existing.parameters != parse_velocity_parameters(R001_PARAMETERS):
        raise MonitoringError(
            "R001 version 1 is already stored with different parameters. "
            "A new threshold requires a new rule version."
        )
    if existing.severity != R001_SEVERITY:
        raise MonitoringError("R001 version 1 severity does not match the registered version")
    return existing


def run_r001_replay(connection: Any) -> MonitoringRun:
    """Register R001 version 1 if needed, then replay it.

    ``triggered_at`` is the event time of the debit that reached the threshold,
    not the time the replay was executed. Running the replay again keeps the
    same alert references and does not add a second alert for the same burst.
    """

    register_r001(connection)
    return run_replay(connection, R001_CODE, R001_VERSION)


def run_replay(connection: Any, rule_code: str, version_number: int) -> MonitoringRun:
    """Replay one active rule version over stored event times.

    The threshold comes from that version's parameter document. This is a
    historical replay: it does not start live monitoring, and a transaction
    is in scope only when its event time falls inside the version's
    effective window.
    """

    started = time.perf_counter()
    database_name = _guard_connection(connection)
    rule = _load_rule(connection, rule_code, version_number)
    if rule is None:
        raise MonitoringError(f"{rule_code} version {version_number} is not registered")
    observed = _load_transactions(connection, rule)
    bursts = find_velocity_bursts(observed, rule.parameters)
    recorded = 0
    already = 0
    with connection.transaction():
        for burst in bursts:
            if _store_burst(connection, rule, burst):
                recorded += 1
            else:
                already += 1
    stored_alerts, accounts, links = _stored_counts(connection, rule)
    return MonitoringRun(
        mode=RETROSPECTIVE_REPLAY,
        database_name=database_name,
        monitoring_rule_id=rule.monitoring_rule_id,
        rule_code=rule.rule_code,
        version_number=rule.version_number,
        transactions_considered=len(observed),
        alerts_recorded=recorded,
        alerts_already_present=already,
        stored_alerts=stored_alerts,
        affected_accounts=accounts,
        linked_transactions=links,
        elapsed_seconds=time.perf_counter() - started,
    )


def _guard_connection(connection: Any) -> str:
    row = connection.execute("SELECT current_database()").fetchone()
    database_name = row[0]
    assert_replay_database(database_name)
    return database_name


def _insert_r001(connection: Any) -> None:
    from psycopg.types.json import Json

    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO amlog.monitoring_rules (
                rule_code,
                version_number,
                rule_name,
                description,
                parameter_document,
                severity,
                lifecycle_status,
                effective_from,
                authored_by
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'draft', %s, %s)
            RETURNING monitoring_rule_id
            """,
            (
                R001_CODE,
                R001_VERSION,
                R001_NAME,
                R001_DESCRIPTION,
                Json(R001_PARAMETERS),
                R001_SEVERITY,
                R001_EFFECTIVE_FROM,
                R001_AUTHORED_BY,
            ),
        ).fetchone()
        connection.execute(
            """
            UPDATE amlog.monitoring_rules
               SET lifecycle_status = 'active'
             WHERE monitoring_rule_id = %s
               AND lifecycle_status = 'draft'
            """,
            (row[0],),
        )


def _load_rule(connection: Any, rule_code: str, version_number: int) -> RuleVersion | None:
    row = connection.execute(
        """
        SELECT
            monitoring_rule_id,
            rule_code,
            version_number,
            severity,
            lifecycle_status,
            effective_from,
            effective_to,
            parameter_document
        FROM amlog.monitoring_rules
        WHERE rule_code = %s
          AND version_number = %s
        """,
        (rule_code, version_number),
    ).fetchone()
    if row is None:
        return None
    if row[4] != "active":
        raise MonitoringError(
            f"{rule_code} version {version_number} is {row[4]}, not active"
        )
    if row[5] is None:
        raise MonitoringError("an active rule version needs effective_from")
    return RuleVersion(
        monitoring_rule_id=row[0],
        rule_code=row[1],
        version_number=row[2],
        severity=row[3],
        effective_from=row[5],
        effective_to=row[6],
        parameters=parse_velocity_parameters(row[7]),
    )


def _load_transactions(connection: Any, rule: RuleVersion) -> tuple[ObservedTransaction, ...]:
    rows = connection.execute(
        TRANSACTION_QUERY,
        (
            rule.parameters.direction,
            rule.effective_from,
            rule.effective_to,
            rule.effective_to,
        ),
    ).fetchall()
    return tuple(
        ObservedTransaction(
            transaction_id=row[0],
            account_id=row[1],
            customer_id=row[2],
            direction=row[3],
            event_time=row[4],
        )
        for row in rows
    )


def _store_burst(connection: Any, rule: RuleVersion, burst: VelocityBurst) -> bool:
    reference = alert_reference(rule, burst)
    summary = (
        "Historical replay of "
        f"{rule.rule_code} version {rule.version_number}. "
        f"{len(burst.members)} {rule.parameters.direction} transactions on this account "
        f"fell between {burst.window_started_at.isoformat()} and "
        f"{burst.window_ended_at.isoformat()}. "
        "This row was not produced by live monitoring."
    )
    inserted = connection.execute(
        """
        INSERT INTO amlog.alerts (
            alert_reference,
            monitoring_rule_id,
            recorded_rule_code,
            recorded_rule_version,
            customer_id,
            account_id,
            triggered_at,
            window_started_at,
            window_ended_at,
            severity,
            lifecycle_status,
            summary
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
        ON CONFLICT (alert_reference) DO NOTHING
        RETURNING alert_id
        """,
        (
            reference,
            rule.monitoring_rule_id,
            rule.rule_code,
            rule.version_number,
            burst.customer_id,
            burst.account_id,
            burst.triggering.event_time,
            burst.window_started_at,
            burst.window_ended_at,
            rule.severity,
            summary,
        ),
    ).fetchone()
    if inserted is None:
        return False
    alert_id = inserted[0]
    for member in burst.members:
        role = "triggering" if member.transaction_id == burst.triggering.transaction_id else "supporting"
        connection.execute(
            """
            INSERT INTO amlog.alert_transactions (
                alert_id,
                transaction_id,
                relationship_role
            )
            VALUES (%s, %s, %s)
            """,
            (alert_id, member.transaction_id, role),
        )
    _audit_alert(connection, alert_id, reference, rule, burst)
    return True


def alert_reference(rule: RuleVersion, burst: VelocityBurst) -> str:
    """Stable identity of one burst under one rule version.

    The same account, rule version, and triggering transaction always produce
    the same reference. A second replay therefore finds the stored alert
    instead of opening another one.
    """

    return (
        f"{rule.rule_code}-v{rule.version_number}-"
        f"{burst.account_id}-{burst.triggering.transaction_id}"
    )


def _audit_alert(
    connection: Any,
    alert_id: UUID,
    reference: str,
    rule: RuleVersion,
    burst: VelocityBurst,
) -> None:
    from psycopg.types.json import Json

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
            "amlog.monitoring",
            "alert_recorded",
            "amlog",
            "alerts",
            alert_id,
            reference,
            Json(
                {
                    "alert_reference": reference,
                    "rule_code": rule.rule_code,
                    "rule_version": rule.version_number,
                    "account_id": str(burst.account_id),
                    "triggering_transaction_id": str(burst.triggering.transaction_id),
                    "contributing_count": len(burst.members),
                    "mode": RETROSPECTIVE_REPLAY,
                }
            ),
            Json(
                {
                    "mode": RETROSPECTIVE_REPLAY,
                    "window_seconds": rule.parameters.window_seconds,
                    "minimum_count": rule.parameters.minimum_count,
                    "direction": rule.parameters.direction,
                }
            ),
        ),
    )


def _stored_counts(connection: Any, rule: RuleVersion) -> tuple[int, int, int]:
    alerts = connection.execute(
        """
        SELECT count(*), count(DISTINCT account_id)
        FROM amlog.alerts
        WHERE monitoring_rule_id = %s
        """,
        (rule.monitoring_rule_id,),
    ).fetchone()
    links = connection.execute(
        """
        SELECT count(*)
        FROM amlog.alert_transactions AS link
        JOIN amlog.alerts AS alert ON alert.alert_id = link.alert_id
        WHERE alert.monitoring_rule_id = %s
        """,
        (rule.monitoring_rule_id,),
    ).fetchone()
    return int(alerts[0]), int(alerts[1]), int(links[0])


@dataclass(frozen=True)
class TurnoverRuleVersion:
    """An active R002 version. Its parameters are not a velocity document."""

    monitoring_rule_id: UUID
    rule_code: str
    version_number: int
    severity: str
    effective_from: datetime
    effective_to: datetime | None
    parameters: TurnoverParameters


# Debits and credits. Amounts stay in the stored transaction currency.
# Scenario notes, labels, and foreign-exchange fields are not inputs.
TURNOVER_TRANSACTION_QUERY = """
SELECT
    transaction_id,
    account_id,
    customer_id,
    direction,
    event_timestamp,
    amount,
    btrim(currency_code::text)
FROM amlog.transactions
WHERE event_timestamp >= %s
  AND (%s::timestamptz IS NULL OR event_timestamp < %s)
ORDER BY account_id, event_timestamp, transaction_id
"""


def register_r002(connection: Any) -> TurnoverRuleVersion:
    """Insert R002 version 1 when it is absent, then return the active row.

    An existing version with different parameters is left unchanged and refused.
    This does not read or update R001.
    """

    _guard_connection(connection)
    existing = _load_turnover_rule(connection, R002_CODE, R002_VERSION)
    if existing is None:
        _insert_r002(connection)
        existing = _load_turnover_rule(connection, R002_CODE, R002_VERSION)
    if existing is None:
        raise MonitoringError("R002 version 1 could not be read after registration")
    if existing.parameters != parse_turnover_parameters(R002_PARAMETERS):
        raise MonitoringError(
            "R002 version 1 is already stored with different parameters. "
            "A new threshold requires a new rule version."
        )
    if existing.severity != R002_SEVERITY:
        raise MonitoringError("R002 version 1 severity does not match the registered version")
    return existing


def run_r002_replay(connection: Any) -> MonitoringRun:
    """Register R002 version 1 if needed, then replay it.

    ``triggered_at`` is the event time of the outgoing payment that reached
    the ratio. The incoming payment is linked as context. Running the replay
    again keeps the same alert reference for that incoming payment.
    """

    register_r002(connection)
    started = time.perf_counter()
    database_name = _guard_connection(connection)
    rule = _load_turnover_rule(connection, R002_CODE, R002_VERSION)
    if rule is None:
        raise MonitoringError("R002 version 1 is not registered")
    observed = _load_turnover_transactions(connection, rule)
    episodes = find_turnover_episodes(observed, rule.parameters)
    recorded = 0
    already = 0
    with connection.transaction():
        for episode in episodes:
            if _store_episode(connection, rule, episode):
                recorded += 1
            else:
                already += 1
    stored_alerts, accounts, links = _stored_turnover_counts(connection, rule)
    return MonitoringRun(
        mode=RETROSPECTIVE_REPLAY,
        database_name=database_name,
        monitoring_rule_id=rule.monitoring_rule_id,
        rule_code=rule.rule_code,
        version_number=rule.version_number,
        transactions_considered=len(observed),
        alerts_recorded=recorded,
        alerts_already_present=already,
        stored_alerts=stored_alerts,
        affected_accounts=accounts,
        linked_transactions=links,
        elapsed_seconds=time.perf_counter() - started,
    )


def _insert_r002(connection: Any) -> None:
    from psycopg.types.json import Json

    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO amlog.monitoring_rules (
                rule_code,
                version_number,
                rule_name,
                description,
                parameter_document,
                severity,
                lifecycle_status,
                effective_from,
                authored_by
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'draft', %s, %s)
            RETURNING monitoring_rule_id
            """,
            (
                R002_CODE,
                R002_VERSION,
                R002_NAME,
                R002_DESCRIPTION,
                Json(R002_PARAMETERS),
                R002_SEVERITY,
                R002_EFFECTIVE_FROM,
                R002_AUTHORED_BY,
            ),
        ).fetchone()
        connection.execute(
            """
            UPDATE amlog.monitoring_rules
               SET lifecycle_status = 'active'
             WHERE monitoring_rule_id = %s
               AND lifecycle_status = 'draft'
            """,
            (row[0],),
        )


def _load_turnover_rule(
    connection: Any,
    rule_code: str,
    version_number: int,
) -> TurnoverRuleVersion | None:
    row = connection.execute(
        """
        SELECT
            monitoring_rule_id,
            rule_code,
            version_number,
            severity,
            lifecycle_status,
            effective_from,
            effective_to,
            parameter_document
        FROM amlog.monitoring_rules
        WHERE rule_code = %s
          AND version_number = %s
        """,
        (rule_code, version_number),
    ).fetchone()
    if row is None:
        return None
    if row[4] != "active":
        raise MonitoringError(f"{rule_code} version {version_number} is {row[4]}, not active")
    if row[5] is None:
        raise MonitoringError("an active rule version needs effective_from")
    return TurnoverRuleVersion(
        monitoring_rule_id=row[0],
        rule_code=row[1],
        version_number=row[2],
        severity=row[3],
        effective_from=row[5],
        effective_to=row[6],
        parameters=parse_turnover_parameters(row[7]),
    )


def _load_turnover_transactions(
    connection: Any,
    rule: TurnoverRuleVersion,
) -> tuple[TurnoverTransaction, ...]:
    rows = connection.execute(
        TURNOVER_TRANSACTION_QUERY,
        (rule.effective_from, rule.effective_to, rule.effective_to),
    ).fetchall()
    return tuple(
        TurnoverTransaction(
            transaction_id=row[0],
            account_id=row[1],
            customer_id=row[2],
            direction=row[3],
            event_time=row[4],
            amount=row[5],
            currency_code=row[6],
        )
        for row in rows
    )


def _store_episode(connection: Any, rule: TurnoverRuleVersion, episode: TurnoverEpisode) -> bool:
    reference = turnover_alert_reference(rule, episode)
    summary = (
        "Historical replay of "
        f"{rule.rule_code} version {rule.version_number}. "
        f"Inferred turnover in {episode.incoming.currency_code}: incoming "
        f"{_money(episode.incoming.amount)} at {episode.incoming.event_time.isoformat()} "
        f"was followed by allocated outgoing {_money(episode.allocated_outgoing)} "
        f"by {episode.triggering.event_time.isoformat()}. "
        "The comparison uses same-currency amounts. "
        "It does not prove that the same funds were transferred. "
        "This row was not produced by live monitoring."
    )
    inserted = connection.execute(
        """
        INSERT INTO amlog.alerts (
            alert_reference,
            monitoring_rule_id,
            recorded_rule_code,
            recorded_rule_version,
            customer_id,
            account_id,
            triggered_at,
            window_started_at,
            window_ended_at,
            severity,
            lifecycle_status,
            summary
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
        ON CONFLICT (alert_reference) DO NOTHING
        RETURNING alert_id
        """,
        (
            reference,
            rule.monitoring_rule_id,
            rule.rule_code,
            rule.version_number,
            episode.customer_id,
            episode.account_id,
            episode.triggering.event_time,
            episode.window_started_at,
            episode.window_ended_at,
            rule.severity,
            summary,
        ),
    ).fetchone()
    if inserted is None:
        return False
    alert_id = inserted[0]
    connection.execute(
        """
        INSERT INTO amlog.alert_transactions (alert_id, transaction_id, relationship_role)
        VALUES (%s, %s, 'contextual')
        """,
        (alert_id, episode.incoming.transaction_id),
    )
    for part in episode.outgoing:
        role = (
            "triggering"
            if part.transaction.transaction_id == episode.triggering.transaction_id
            else "supporting"
        )
        connection.execute(
            """
            INSERT INTO amlog.alert_transactions (alert_id, transaction_id, relationship_role)
            VALUES (%s, %s, %s)
            """,
            (alert_id, part.transaction.transaction_id, role),
        )
    _audit_turnover(connection, alert_id, reference, rule, episode)
    return True


def turnover_alert_reference(rule: TurnoverRuleVersion, episode: TurnoverEpisode) -> str:
    """Stable identity of one incoming-payment episode under one rule version."""

    return (
        f"{rule.rule_code}-v{rule.version_number}-"
        f"{episode.account_id}-{episode.incoming.transaction_id}"
    )


def _money(amount: Decimal) -> str:
    return format(amount, "f")


def _audit_turnover(
    connection: Any,
    alert_id: UUID,
    reference: str,
    rule: TurnoverRuleVersion,
    episode: TurnoverEpisode,
) -> None:
    from psycopg.types.json import Json

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
            "amlog.monitoring",
            "alert_recorded",
            "amlog",
            "alerts",
            alert_id,
            reference,
            Json(
                {
                    "alert_reference": reference,
                    "rule_code": rule.rule_code,
                    "rule_version": rule.version_number,
                    "account_id": str(episode.account_id),
                    "incoming_transaction_id": str(episode.incoming.transaction_id),
                    "triggering_transaction_id": str(episode.triggering.transaction_id),
                    "allocated_outgoing": _money(episode.allocated_outgoing),
                    "currency_code": episode.incoming.currency_code,
                    "inference": "same_currency_amount_comparison",
                    "mode": RETROSPECTIVE_REPLAY,
                }
            ),
            Json(
                {
                    "mode": RETROSPECTIVE_REPLAY,
                    "window_seconds": rule.parameters.window_seconds,
                    "minimum_incoming_amount": _money(rule.parameters.minimum_incoming_amount),
                    "outgoing_ratio": _money(rule.parameters.outgoing_ratio),
                    "amount_basis": rule.parameters.amount_basis,
                }
            ),
        ),
    )


def _stored_turnover_counts(connection: Any, rule: TurnoverRuleVersion) -> tuple[int, int, int]:
    alerts = connection.execute(
        """
        SELECT count(*), count(DISTINCT account_id)
        FROM amlog.alerts
        WHERE monitoring_rule_id = %s
        """,
        (rule.monitoring_rule_id,),
    ).fetchone()
    links = connection.execute(
        """
        SELECT count(*)
        FROM amlog.alert_transactions AS link
        JOIN amlog.alerts AS alert ON alert.alert_id = link.alert_id
        WHERE alert.monitoring_rule_id = %s
        """,
        (rule.monitoring_rule_id,),
    ).fetchone()
    return int(alerts[0]), int(alerts[1]), int(links[0])

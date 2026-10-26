"""Persist investigator review without rewriting earlier decisions.

The service uses the existing case, note, audit, and label tables. It does
not read scenario notes. It reads a stored anomaly rank when one exists. It
does not fit a model, and it does not mark a label eligible for training.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from amlog.prioritize.names import ISOLATION_MODEL_NAME

PROTECTED_DATABASES = frozenset({"amlog", "postgres", "template0", "template1"})

DISPOSITIONS = (
    "no_further_action",
    "continue_monitoring",
    "escalated",
    "inconclusive",
)

# Reviewer words, and the label value stored for that word.
# inconclusive here means the reviewer could not judge usefulness.
# It is not the operational disposition of the same spelling.
USEFULNESS = {
    "productive": "productive_alert",
    "non_productive": "non_productive_alert",
    "inconclusive": "uncertain",
}

_ASSESSMENT_STATEMENT = (
    "Investigator assessment entered in this review. "
    "It records whether the alert was useful to review. "
    "It is not a finding that a crime occurred or that none occurred. "
    "The stored activity may be development history. "
    "This row is not eligible for model training."
)


class ReviewError(ValueError):
    """The review action conflicts with the stored case or the request."""


@dataclass(frozen=True)
class Readiness:
    """How many human outcomes exist for a later training decision."""

    alerts: int
    alerts_with_a_case: int
    closed_cases: int
    investigator_assessments: int
    eligible_evaluation_labels: int
    usable_training_outcomes: int


@dataclass(frozen=True)
class AlertSummary:
    alert_id: UUID
    alert_reference: str
    rule_code: str
    rule_version: int
    severity: str
    triggered_at: datetime
    customer_reference: str
    account_reference: str | None
    case_status: str | None
    disposition: str | None
    priority_rank: int | None = None
    priority_score: Decimal | None = None


@dataclass(frozen=True)
class LinkedPayment:
    transaction_id: UUID
    role: str
    direction: str
    amount: Decimal
    currency_code: str
    payment_channel: str
    event_time: datetime
    counterparty_name: str | None


@dataclass(frozen=True)
class FeatureRow:
    name: str
    value: str


@dataclass(frozen=True)
class NoteRow:
    author_reference: str
    note_text: str
    noted_at: datetime


@dataclass(frozen=True)
class AssessmentRow:
    label_value: str
    labeled_by: str
    labeled_at: datetime
    eligible_for_evaluation: bool
    data_origin: str
    rationale: str | None


@dataclass(frozen=True)
class CaseView:
    investigation_case_id: UUID
    case_reference: str
    lifecycle_status: str
    disposition: str | None
    assigned_to: str | None
    status_summary: str | None
    opened_at: datetime
    closed_at: datetime | None
    notes: tuple[NoteRow, ...]


@dataclass(frozen=True)
class ScoreView:
    """One stored experiment score. It is not an investigation outcome."""

    model_name: str
    version_label: str
    algorithm_family: str | None
    lifecycle_status: str
    score: Decimal
    score_semantics: str
    score_use: str
    explanation: dict[str, Any]


@dataclass(frozen=True)
class AlertDetail:
    summary: AlertSummary
    customer_id: UUID
    account_id: UUID | None
    customer_name: str
    window_started_at: datetime | None
    window_ended_at: datetime | None
    alert_summary: str | None
    payments: tuple[LinkedPayment, ...]
    features: tuple[FeatureRow, ...]
    case: CaseView | None
    assessments: tuple[AssessmentRow, ...]
    scores: tuple[ScoreView, ...] = ()


def assert_review_database(database_name: str) -> None:
    """Refuse the development database and PostgreSQL's own databases."""

    if database_name in PROTECTED_DATABASES:
        raise ReviewError(
            f"{database_name} is not a review database. No investigation row was written."
        )


def list_alerts(
    connection: Any,
    *,
    rule_code: str | None = None,
    review_state: str = "all",
    sort: str = "time",
    limit: int = 40,
    offset: int = 0,
) -> tuple[AlertSummary, ...]:
    """Return one page of alerts. The query does not read scenario notes."""

    _guard(connection)
    if review_state not in {"all", "unreviewed", "open", "closed"}:
        raise ReviewError("review_state must be all, unreviewed, open, or closed.")
    if sort not in {"time", "priority"}:
        raise ReviewError("sort must be time or priority.")
    rows = connection.execute(
        """
        SELECT
            alerts.alert_id,
            alerts.alert_reference,
            alerts.recorded_rule_code,
            alerts.recorded_rule_version,
            alerts.severity,
            alerts.triggered_at,
            customers.customer_reference,
            accounts.account_reference,
            investigation_cases.lifecycle_status,
            investigation_cases.disposition,
            priority.anomaly_rank,
            priority.anomaly_score
        FROM amlog.alerts AS alerts
        JOIN amlog.customers AS customers ON customers.customer_id = alerts.customer_id
        LEFT JOIN amlog.accounts AS accounts ON accounts.account_id = alerts.account_id
        LEFT JOIN amlog.investigation_cases AS investigation_cases
          ON investigation_cases.investigation_case_id = alerts.investigation_case_id
        LEFT JOIN LATERAL (
            SELECT
                risk_scores.score AS anomaly_score,
                NULLIF(risk_scores.explanation_document ->> 'rank', '')::integer AS anomaly_rank
            FROM amlog.risk_scores AS risk_scores
            JOIN amlog.model_versions AS model_versions
              ON model_versions.model_version_id = risk_scores.model_version_id
            WHERE risk_scores.alert_id = alerts.alert_id
              AND model_versions.model_name = %s
              AND risk_scores.score_use = 'experiment'
            ORDER BY risk_scores.scored_at DESC, risk_scores.risk_score_id
            LIMIT 1
        ) AS priority ON TRUE
        WHERE (%s::text IS NULL OR alerts.recorded_rule_code = %s)
          AND (
            %s = 'all'
            OR (%s = 'unreviewed' AND alerts.investigation_case_id IS NULL)
            OR (%s = 'open' AND investigation_cases.lifecycle_status IS NOT NULL
                AND investigation_cases.lifecycle_status <> 'closed')
            OR (%s = 'closed' AND investigation_cases.lifecycle_status = 'closed')
          )
        ORDER BY
            CASE WHEN %s = 'priority' THEN priority.anomaly_rank END ASC NULLS LAST,
            alerts.triggered_at,
            alerts.alert_id
        LIMIT %s OFFSET %s
        """,
        (
            ISOLATION_MODEL_NAME,
            rule_code,
            rule_code,
            review_state,
            review_state,
            review_state,
            review_state,
            sort,
            limit,
            offset,
        ),
    ).fetchall()
    return tuple(_summary(row) for row in rows)


def count_alerts(connection: Any, *, rule_code: str | None = None, review_state: str = "all") -> int:
    _guard(connection)
    row = connection.execute(
        """
        SELECT count(*)
        FROM amlog.alerts AS alerts
        LEFT JOIN amlog.investigation_cases AS investigation_cases
          ON investigation_cases.investigation_case_id = alerts.investigation_case_id
        WHERE (%s::text IS NULL OR alerts.recorded_rule_code = %s)
          AND (
            %s = 'all'
            OR (%s = 'unreviewed' AND alerts.investigation_case_id IS NULL)
            OR (%s = 'open' AND investigation_cases.lifecycle_status IS NOT NULL
                AND investigation_cases.lifecycle_status <> 'closed')
            OR (%s = 'closed' AND investigation_cases.lifecycle_status = 'closed')
          )
        """,
        (rule_code, rule_code, review_state, review_state, review_state, review_state),
    ).fetchone()
    return int(row[0])


def get_alert(connection: Any, alert_id: UUID) -> AlertDetail:
    """Load one alert, its own account activity, and any review already stored."""

    _guard(connection)
    header = connection.execute(
        """
        SELECT
            alerts.alert_id,
            alerts.alert_reference,
            alerts.recorded_rule_code,
            alerts.recorded_rule_version,
            alerts.severity,
            alerts.triggered_at,
            customers.customer_reference,
            accounts.account_reference,
            investigation_cases.lifecycle_status,
            investigation_cases.disposition,
            alerts.customer_id,
            alerts.account_id,
            customers.legal_name,
            alerts.window_started_at,
            alerts.window_ended_at,
            alerts.summary,
            alerts.investigation_case_id
        FROM amlog.alerts AS alerts
        JOIN amlog.customers AS customers ON customers.customer_id = alerts.customer_id
        LEFT JOIN amlog.accounts AS accounts ON accounts.account_id = alerts.account_id
        LEFT JOIN amlog.investigation_cases AS investigation_cases
          ON investigation_cases.investigation_case_id = alerts.investigation_case_id
        WHERE alerts.alert_id = %s
        """,
        (alert_id,),
    ).fetchone()
    if header is None:
        raise ReviewError("That alert was not found.")
    account_id = header[11]
    payments = _payments(connection, alert_id, account_id)
    features = _features(connection, alert_id)
    case = _case(connection, header[16]) if header[16] is not None else None
    assessments = _assessments(connection, alert_id)
    return AlertDetail(
        summary=_summary(header[:10]),
        customer_id=header[10],
        account_id=account_id,
        customer_name=header[12],
        window_started_at=header[13],
        window_ended_at=header[14],
        alert_summary=header[15],
        payments=payments,
        features=features,
        case=case,
        assessments=assessments,
        scores=_priority_scores(connection, alert_id),
    )


def open_case(connection: Any, alert_id: UUID, reviewer: str) -> UUID:
    """Open one case for the alert, or return the case already linked to it."""

    reviewer = _reviewer(reviewer)
    _guard(connection)
    with connection.transaction():
        alert = _lock_alert(connection, alert_id)
        if alert["case_id"] is not None:
            return alert["case_id"]
        opened_at = datetime.now(timezone.utc)
        case_reference = f"INV-{alert_id}"
        case_id = connection.execute(
            """
            INSERT INTO amlog.investigation_cases (
                case_reference, customer_id, lifecycle_status, priority,
                assigned_to, status_summary, opened_at
            )
            VALUES (%s, %s, 'open', 'routine', %s, %s, %s)
            RETURNING investigation_case_id
            """,
            (
                case_reference,
                alert["customer_id"],
                reviewer,
                "Investigation opened. No outcome has been recorded.",
                opened_at,
            ),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE amlog.alerts
            SET investigation_case_id = %s
            WHERE alert_id = %s
              AND investigation_case_id IS NULL
            """,
            (case_id, alert_id),
        )
        _audit(
            connection,
            reviewer,
            "case_opened",
            "amlog",
            "investigation_cases",
            case_id,
            None,
            {"lifecycle_status": "open", "case_reference": case_reference},
            {"alert_id": str(alert_id)},
        )
    connection.commit()
    return case_id


def add_finding(connection: Any, alert_id: UUID, reviewer: str, text: str) -> UUID:
    """Append a finding. Earlier notes stay."""

    reviewer = _reviewer(reviewer)
    text = _text(text)
    _guard(connection)
    with connection.transaction():
        case_id = _require_open_case(connection, alert_id)
        note_id = _insert_note(connection, case_id, reviewer, text)
        connection.execute(
            """
            UPDATE amlog.investigation_cases
            SET lifecycle_status = 'in_progress',
                status_summary = 'Finding recorded. No outcome has been closed.'
            WHERE investigation_case_id = %s
              AND lifecycle_status <> 'closed'
            """,
            (case_id,),
        )
        _audit(
            connection,
            reviewer,
            "finding_recorded",
            "amlog",
            "investigation_notes",
            note_id,
            None,
            {"note_id": str(note_id)},
            {"alert_id": str(alert_id)},
        )
    connection.commit()
    return note_id


def record_assessment(connection: Any, alert_id: UUID, reviewer: str, usefulness: str, rationale: str) -> UUID:
    """Append an assessment label. It is not eligible for training."""

    reviewer = _reviewer(reviewer)
    rationale = _text(rationale)
    label_value = _usefulness(usefulness)
    full_rationale = f"{_ASSESSMENT_STATEMENT} Reviewer rationale: {rationale}"
    _guard(connection)
    with connection.transaction():
        case_id = _require_open_case(connection, alert_id)
        latest = _latest_assessment(connection, alert_id)
        if latest is not None and latest[0] == label_value and latest[1] == full_rationale:
            raise ReviewError(
                "That assessment is already the current one. A revision needs a different judgment or rationale."
            )
        note_text = f"Usefulness assessment: {usefulness}. {rationale}"
        _insert_note(connection, case_id, reviewer, note_text)
        labeled_at = datetime.now(timezone.utc)
        label_id = connection.execute(
            """
            INSERT INTO amlog_label.alert_labels (
                alert_id, label_value, label_source, data_origin, label_rationale,
                labeled_at, labeled_by, eligible_for_evaluation
            )
            VALUES (%s, %s, 'investigator_outcome', 'operational', %s, %s, %s, false)
            RETURNING alert_label_id
            """,
            (alert_id, label_value, full_rationale, labeled_at, reviewer),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE amlog.investigation_cases
            SET lifecycle_status = 'in_progress',
                assigned_to = %s,
                status_summary = %s
            WHERE investigation_case_id = %s
              AND lifecycle_status <> 'closed'
            """,
            (reviewer, f"Current usefulness assessment: {usefulness}. Case is not closed.", case_id),
        )
        _audit(
            connection,
            reviewer,
            "assessment_recorded",
            "amlog_label",
            "alert_labels",
            label_id,
            None,
            {
                "label_value": label_value,
                "usefulness": usefulness,
                "eligible_for_evaluation": False,
                "data_origin": "operational",
            },
            {"alert_id": str(alert_id), "statement": _ASSESSMENT_STATEMENT},
        )
    connection.commit()
    return label_id


def close_case(connection: Any, alert_id: UUID, reviewer: str, disposition: str, rationale: str) -> None:
    """Close the case with an operational disposition. The assessment is separate."""

    reviewer = _reviewer(reviewer)
    rationale = _text(rationale)
    if disposition not in DISPOSITIONS:
        raise ReviewError("The operational outcome must be one of the stored disposition values.")
    _guard(connection)
    with connection.transaction():
        case = _require_open_case_row(connection, alert_id)
        if _latest_assessment(connection, alert_id) is None:
            raise ReviewError(
                "Record whether the alert was useful to review before closing the case."
            )
        previous = {
            "lifecycle_status": case["lifecycle_status"],
            "disposition": case["disposition"],
            "status_summary": case["status_summary"],
        }
        closed_at = datetime.now(timezone.utc)
        summary = f"Closed with operational outcome {disposition}."
        connection.execute(
            """
            UPDATE amlog.investigation_cases
            SET lifecycle_status = 'closed',
                disposition = %s,
                closed_at = %s,
                assigned_to = %s,
                status_summary = %s
            WHERE investigation_case_id = %s
            """,
            (disposition, closed_at, reviewer, summary, case["case_id"]),
        )
        _insert_note(
            connection,
            case["case_id"],
            reviewer,
            f"Case closed. Operational outcome: {disposition}. {rationale}",
        )
        _audit(
            connection,
            reviewer,
            "case_closed",
            "amlog",
            "investigation_cases",
            case["case_id"],
            previous,
            {"lifecycle_status": "closed", "disposition": disposition, "status_summary": summary},
            {"alert_id": str(alert_id)},
        )
    connection.commit()


def reopen_case(connection: Any, alert_id: UUID, reviewer: str, rationale: str) -> None:
    """Reopen a closed case. Notes, assessments, and the prior disposition stay in history."""

    reviewer = _reviewer(reviewer)
    rationale = _text(rationale)
    _guard(connection)
    with connection.transaction():
        alert = _lock_alert(connection, alert_id)
        if alert["case_id"] is None:
            raise ReviewError("Open an investigation before revising it.")
        case = connection.execute(
            """
            SELECT lifecycle_status, disposition, status_summary
            FROM amlog.investigation_cases
            WHERE investigation_case_id = %s
            FOR UPDATE
            """,
            (alert["case_id"],),
        ).fetchone()
        if case[0] != "closed":
            raise ReviewError("Only a closed case can be reopened.")
        previous = {
            "lifecycle_status": case[0],
            "disposition": case[1],
            "status_summary": case[2],
        }
        summary = f"Reopened. Previous operational outcome was {case[1]}."
        connection.execute(
            """
            UPDATE amlog.investigation_cases
            SET lifecycle_status = 'in_progress',
                disposition = NULL,
                closed_at = NULL,
                assigned_to = %s,
                status_summary = %s
            WHERE investigation_case_id = %s
            """,
            (reviewer, summary, alert["case_id"]),
        )
        _insert_note(
            connection,
            alert["case_id"],
            reviewer,
            f"Case reopened. Previous operational outcome: {case[1]}. {rationale}",
        )
        _audit(
            connection,
            reviewer,
            "case_reopened",
            "amlog",
            "investigation_cases",
            alert["case_id"],
            previous,
            {"lifecycle_status": "in_progress", "disposition": None, "status_summary": summary},
            {"alert_id": str(alert_id)},
        )
    connection.commit()


def readiness(connection: Any) -> Readiness:
    """Count human outcomes. An ineligible assessment is not a training row."""

    _guard(connection)
    alerts = connection.execute("SELECT count(*) FROM amlog.alerts").fetchone()[0]
    with_case = connection.execute(
        "SELECT count(*) FROM amlog.alerts WHERE investigation_case_id IS NOT NULL"
    ).fetchone()[0]
    closed = connection.execute(
        "SELECT count(*) FROM amlog.investigation_cases WHERE lifecycle_status = 'closed'"
    ).fetchone()[0]
    assessments = connection.execute(
        """
        SELECT count(*)
        FROM amlog_label.alert_labels
        WHERE label_source = 'investigator_outcome'
        """
    ).fetchone()[0]
    eligible = connection.execute(
        "SELECT count(*) FROM amlog_label.alert_labels WHERE eligible_for_evaluation"
    ).fetchone()[0]
    usable = connection.execute(
        """
        SELECT count(*)
        FROM amlog_label.alert_labels
        WHERE eligible_for_evaluation
          AND label_source IN ('investigator_outcome', 'quality_review')
          AND label_value IN ('productive_alert', 'non_productive_alert', 'uncertain')
        """
    ).fetchone()[0]
    return Readiness(
        alerts=int(alerts),
        alerts_with_a_case=int(with_case),
        closed_cases=int(closed),
        investigator_assessments=int(assessments),
        eligible_evaluation_labels=int(eligible),
        usable_training_outcomes=int(usable),
    )


def _guard(connection: Any) -> None:
    name = connection.execute("SELECT current_database()").fetchone()[0]
    assert_review_database(str(name))


def _summary(row: tuple) -> AlertSummary:
    return AlertSummary(
        alert_id=row[0],
        alert_reference=row[1],
        rule_code=row[2],
        rule_version=int(row[3]),
        severity=row[4],
        triggered_at=row[5],
        customer_reference=row[6],
        account_reference=row[7],
        case_status=row[8],
        disposition=row[9],
        priority_rank=None if len(row) < 11 or row[10] is None else int(row[10]),
        priority_score=None if len(row) < 12 or row[11] is None else row[11],
    )


def _priority_scores(connection: Any, alert_id: UUID) -> tuple[ScoreView, ...]:
    rows = connection.execute(
        """
        SELECT
            model_versions.model_name,
            model_versions.version_label,
            model_versions.algorithm_family,
            model_versions.lifecycle_status,
            risk_scores.score,
            risk_scores.score_semantics,
            risk_scores.score_use,
            risk_scores.explanation_document
        FROM amlog.risk_scores AS risk_scores
        JOIN amlog.model_versions AS model_versions
          ON model_versions.model_version_id = risk_scores.model_version_id
        WHERE risk_scores.alert_id = %s
          AND risk_scores.score_use = 'experiment'
        ORDER BY risk_scores.scored_at DESC, model_versions.model_name
        """,
        (alert_id,),
    ).fetchall()
    seen: set[str] = set()
    found: list[ScoreView] = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        explanation = row[7] if isinstance(row[7], dict) else {}
        score = row[4] if isinstance(row[4], Decimal) else Decimal(row[4])
        found.append(
            ScoreView(
                model_name=row[0],
                version_label=row[1],
                algorithm_family=row[2],
                lifecycle_status=row[3],
                score=score,
                score_semantics=row[5],
                score_use=row[6],
                explanation=explanation,
            )
        )
    return tuple(found)


def _payments(connection: Any, alert_id: UUID, account_id: UUID | None) -> tuple[LinkedPayment, ...]:
    if account_id is None:
        return ()
    rows = connection.execute(
        """
        SELECT
            transactions.transaction_id,
            alert_transactions.relationship_role,
            transactions.direction,
            transactions.amount,
            btrim(transactions.currency_code),
            transactions.payment_channel,
            transactions.event_timestamp,
            transactions.counterparty_name
        FROM amlog.alert_transactions AS alert_transactions
        JOIN amlog.transactions AS transactions
          ON transactions.transaction_id = alert_transactions.transaction_id
        WHERE alert_transactions.alert_id = %s
          AND transactions.account_id = %s
        ORDER BY transactions.event_timestamp, transactions.transaction_id
        """,
        (alert_id, account_id),
    ).fetchall()
    return tuple(
        LinkedPayment(
            transaction_id=row[0],
            role=row[1],
            direction=row[2],
            amount=row[3] if isinstance(row[3], Decimal) else Decimal(row[3]),
            currency_code=row[4],
            payment_channel=row[5],
            event_time=row[6],
            counterparty_name=row[7],
        )
        for row in rows
    )


def _features(connection: Any, alert_id: UUID) -> tuple[FeatureRow, ...]:
    rows = connection.execute(
        """
        SELECT feature_name, value_type, value_numeric, value_text, value_boolean, value_timestamp
        FROM amlog.alert_features
        WHERE feature_computation_id = (
            SELECT feature_computation_id
            FROM amlog.feature_computations
            WHERE alert_id = %s
            ORDER BY computed_at DESC, feature_computation_id
            LIMIT 1
        )
        ORDER BY feature_name
        """,
        (alert_id,),
    ).fetchall()
    rendered = []
    for name, value_type, numeric, text, boolean, timestamp in rows:
        if value_type == "numeric":
            shown = format(numeric, "f")
        elif value_type == "text":
            shown = text
        elif value_type == "boolean":
            shown = "true" if boolean else "false"
        else:
            shown = timestamp.isoformat()
        rendered.append(FeatureRow(name=name, value=shown))
    return tuple(rendered)


def _case(connection: Any, case_id: UUID) -> CaseView | None:
    row = connection.execute(
        """
        SELECT investigation_case_id, case_reference, lifecycle_status, disposition,
               assigned_to, status_summary, opened_at, closed_at
        FROM amlog.investigation_cases
        WHERE investigation_case_id = %s
        """,
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    notes = connection.execute(
        """
        SELECT author_reference, note_text, noted_at
        FROM amlog.investigation_notes
        WHERE investigation_case_id = %s
        ORDER BY noted_at, investigation_note_id
        """,
        (case_id,),
    ).fetchall()
    return CaseView(
        investigation_case_id=row[0],
        case_reference=row[1],
        lifecycle_status=row[2],
        disposition=row[3],
        assigned_to=row[4],
        status_summary=row[5],
        opened_at=row[6],
        closed_at=row[7],
        notes=tuple(NoteRow(author_reference=item[0], note_text=item[1], noted_at=item[2]) for item in notes),
    )


def _assessments(connection: Any, alert_id: UUID) -> tuple[AssessmentRow, ...]:
    rows = connection.execute(
        """
        SELECT label_value, labeled_by, labeled_at, eligible_for_evaluation, data_origin, label_rationale
        FROM amlog_label.alert_labels
        WHERE alert_id = %s
        ORDER BY labeled_at, alert_label_id
        """,
        (alert_id,),
    ).fetchall()
    return tuple(
        AssessmentRow(
            label_value=row[0],
            labeled_by=row[1],
            labeled_at=row[2],
            eligible_for_evaluation=row[3],
            data_origin=row[4],
            rationale=row[5],
        )
        for row in rows
    )


def _lock_alert(connection: Any, alert_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT customer_id, account_id, investigation_case_id, alert_reference,
               recorded_rule_code, summary, triggered_at
        FROM amlog.alerts
        WHERE alert_id = %s
        FOR UPDATE
        """,
        (alert_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("That alert was not found.")
    return {
        "customer_id": row[0],
        "account_id": row[1],
        "case_id": row[2],
        "alert_reference": row[3],
        "rule_code": row[4],
        "summary": row[5],
        "triggered_at": row[6],
    }


def _require_open_case(connection: Any, alert_id: UUID) -> UUID:
    return _require_open_case_row(connection, alert_id)["case_id"]


def _require_open_case_row(connection: Any, alert_id: UUID) -> dict[str, Any]:
    alert = _lock_alert(connection, alert_id)
    if alert["case_id"] is None:
        raise ReviewError("Open an investigation before recording a finding or an outcome.")
    row = connection.execute(
        """
        SELECT lifecycle_status, disposition, status_summary
        FROM amlog.investigation_cases
        WHERE investigation_case_id = %s
        FOR UPDATE
        """,
        (alert["case_id"],),
    ).fetchone()
    if row[0] == "closed":
        raise ReviewError("Reopen the case before recording a new finding or assessment.")
    return {
        "case_id": alert["case_id"],
        "lifecycle_status": row[0],
        "disposition": row[1],
        "status_summary": row[2],
    }


def _latest_assessment(connection: Any, alert_id: UUID) -> tuple[str, str] | None:
    row = connection.execute(
        """
        SELECT label_value, label_rationale
        FROM amlog_label.alert_labels
        WHERE alert_id = %s
        ORDER BY labeled_at DESC, alert_label_id DESC
        LIMIT 1
        """,
        (alert_id,),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1] or "")


def _insert_note(connection: Any, case_id: UUID, reviewer: str, text: str) -> UUID:
    return connection.execute(
        """
        INSERT INTO amlog.investigation_notes (
            investigation_case_id, author_reference, note_text, noted_at
        )
        VALUES (%s, %s, %s, %s)
        RETURNING investigation_note_id
        """,
        (case_id, reviewer, text, datetime.now(timezone.utc)),
    ).fetchone()[0]


def _audit(
    connection: Any,
    reviewer: str,
    action: str,
    schema: str,
    table: str,
    entity_id: UUID,
    previous: dict | None,
    resulting: dict | None,
    context: dict | None,
) -> None:
    from psycopg.types.json import Json

    connection.execute(
        """
        SELECT amlog.record_audit_event(
            'person'::text,
            %s::text,
            %s::text,
            %s::text,
            %s::text,
            %s::uuid,
            NULL::uuid,
            NULL::text,
            %s::jsonb,
            %s::jsonb,
            %s::jsonb
        )
        """,
        (
            reviewer,
            action,
            schema,
            table,
            entity_id,
            Json(previous) if previous is not None else None,
            Json(resulting) if resulting is not None else None,
            Json(context) if context is not None else None,
        ),
    )


def _reviewer(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200 or any(character in cleaned for character in "\r\n"):
        raise ReviewError("Enter a reviewer reference.")
    return cleaned


def _text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ReviewError("Enter the finding or the reason for the decision.")
    if len(cleaned) > 8000:
        raise ReviewError("The note is too long.")
    return cleaned


def _usefulness(value: str) -> str:
    try:
        return USEFULNESS[value]
    except KeyError as exc:
        raise ReviewError("Usefulness must be productive, non-productive, or inconclusive.") from exc

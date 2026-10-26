"""Load benchmark histories through the existing alert and feature pipeline.

Ingestion, R001, R002, and alert_behavior snapshots are the existing
components. This module does not implement a second monitoring rule. A
judgment is stored only after the replay has linked the case's subject
payment to one alert and that alert has one usable feature snapshot.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from amlog.benchmark.histories import SOURCE_SYSTEM, CaseSpec
from amlog.benchmark.protocol import (
    JUDGMENT_SOURCE,
    PROTOCOL_ACTOR,
    judge,
)
from amlog.features.compute import (
    FEATURE_SET_NAME,
    FEATURE_SET_VERSION,
    AlertView,
    PaymentView,
    compute_snapshot,
)
from amlog.ingest.contract import EVENT_TYPE, SCHEMA_VERSION_2
from amlog.ingest.service import TransactionIngestor
from amlog.monitor.engine import run_r001_replay, run_r002_replay
from amlog.features.store import run_feature_snapshots
_CASE_TABLE = """
CREATE TABLE amlog_benchmark.case_files (
    case_id TEXT PRIMARY KEY,
    population_id TEXT NOT NULL,
    population_role TEXT NOT NULL,
    context_family TEXT NOT NULL,
    account_reference TEXT NOT NULL UNIQUE,
    customer_type TEXT NOT NULL,
    currency_code CHAR(3) NOT NULL,
    arrangement_on_file BOOLEAN NOT NULL,
    arrangement_beneficiary TEXT,
    subject_event_id TEXT NOT NULL,
    document_kind TEXT NOT NULL,
    obtained_after_alert BOOLEAN NOT NULL,
    beneficiary_matches BOOLEAN,
    amount_matches BOOLEAN,
    reference_matches BOOLEAN,
    finding_note TEXT NOT NULL,
    design_id TEXT NOT NULL,
    CHECK (population_role IN ('fit', 'holdout')),
    CHECK (customer_type IN ('individual', 'organization')),
    CHECK (context_family IN (
        'merchant_settlement',
        'established_supplier',
        'unexplained_transfer',
        'new_counterparty'
    )),
    CHECK (document_kind IN ('invoice', 'settlement_report', 'contract', 'not_produced')),
    CHECK (obtained_after_alert),
    CHECK (design_id = 'phase10b-alert-v1'),
    CHECK (char_length(finding_note) > 0)
)
"""

_LINK_TABLE = """
CREATE TABLE amlog_benchmark.alert_links (
    case_id TEXT PRIMARY KEY REFERENCES amlog_benchmark.case_files (case_id),
    alert_id UUID NOT NULL UNIQUE,
    feature_computation_id UUID NOT NULL,
    triggering_transaction_id UUID NOT NULL,
    recorded_rule_code TEXT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    input_fingerprint TEXT NOT NULL
)
"""

_JUDGMENT_TABLE = """
CREATE TABLE amlog_benchmark.judgments (
    case_id TEXT PRIMARY KEY REFERENCES amlog_benchmark.alert_links (case_id),
    outcome TEXT NOT NULL,
    assumption_id TEXT NOT NULL,
    judgment_source TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (outcome IN ('productive_review', 'non_productive_review')),
    CHECK (assumption_id IN ('C1', 'C2')),
    CHECK (judgment_source = 'benchmark_protocol'),
    CHECK (recorded_by = 'amlog.benchmark.protocol'),
    CHECK (char_length(rationale) > 0)
)
"""


class BenchmarkLinkError(ValueError):
    """A history did not become one linked alert with one usable snapshot."""


def prepare_benchmark_schema(connection: Any) -> None:
    """Replace the benchmark schema. Operational tables are left in place."""

    connection.execute("DROP SCHEMA IF EXISTS amlog_benchmark CASCADE")
    connection.execute("CREATE SCHEMA amlog_benchmark")
    connection.execute(_CASE_TABLE)
    connection.execute(_LINK_TABLE)
    connection.execute(_JUDGMENT_TABLE)


def load_histories(connection: Any, cases: tuple[CaseSpec, ...]) -> None:
    """Ingest parties and payments, replay the two rules, and store links."""

    _insert_cases(connection, cases)
    _insert_parties(connection, cases)
    _ingest(connection, cases)
    run_r001_replay(connection)
    run_r002_replay(connection)
    run_feature_snapshots(connection)
    linked = [_resolve(connection, case) for case in cases]
    _insert_links(connection, linked)
    _reject_operational_labels(connection)


def _insert_cases(connection: Any, cases: tuple[CaseSpec, ...]) -> None:
    cursor = connection.cursor()
    cursor.executemany(
        """
        INSERT INTO amlog_benchmark.case_files (
            case_id, population_id, population_role, context_family, account_reference,
            customer_type, currency_code, arrangement_on_file, arrangement_beneficiary,
            subject_event_id, document_kind, obtained_after_alert, beneficiary_matches,
            amount_matches, reference_matches, finding_note, design_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                case.case_id,
                case.population_id,
                case.population_role,
                case.context_family,
                case.account_reference,
                case.customer_type,
                case.currency_code,
                case.arrangement_on_file,
                case.arrangement_beneficiary,
                case.subject_event_id,
                case.finding.document_kind,
                case.finding.obtained_after_alert,
                case.finding.beneficiary_matches,
                case.finding.amount_matches,
                case.finding.reference_matches,
                case.finding.note,
                "phase10b-alert-v1",
            )
            for case in cases
        ],
    )


def _insert_parties(connection: Any, cases: tuple[CaseSpec, ...]) -> None:
    for case in cases:
        connection.execute(
            """
            INSERT INTO amlog.customers (
                customer_reference, customer_type, legal_name,
                residency_country_code, lifecycle_status, onboarded_at
            )
            VALUES (%s, %s, %s, 'US', 'active', %s)
            """,
            (
                case.customer_reference,
                case.customer_type,
                case.customer_reference,
                case.opened_at,
            ),
        )
        customer_id = connection.execute(
            "SELECT customer_id FROM amlog.customers WHERE customer_reference = %s",
            (case.customer_reference,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO amlog.accounts (
                customer_id, account_reference, source_system, source_account_id,
                account_type, currency_code, lifecycle_status, opened_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'open', %s)
            """,
            (
                customer_id,
                case.account_reference,
                SOURCE_SYSTEM,
                case.account_reference,
                "operating" if case.customer_type == "organization" else "personal",
                case.currency_code,
                case.opened_at,
            ),
        )


def _ingest(connection: Any, cases: tuple[CaseSpec, ...]) -> None:
    ingestor = TransactionIngestor(connection)
    for case in cases:
        for payment in case.payments:
            result = ingestor.ingest(_payload(case, payment))
            if result.outcome != "recorded":
                raise BenchmarkLinkError(
                    f"{case.case_id} payment {payment.event_id} was {result.outcome}. "
                    f"{result.message}"
                )


def _payload(case: CaseSpec, payment) -> dict[str, str]:
    event_time = case.anchor + timedelta(minutes=payment.minute_offset)
    return {
        "schema_version": SCHEMA_VERSION_2,
        "event_type": EVENT_TYPE,
        "source_system": SOURCE_SYSTEM,
        "source_event_id": payment.event_id,
        "account_reference": case.account_reference,
        "source_account_id": case.account_reference,
        "transaction_type": payment.transaction_type,
        "payment_channel": payment.channel,
        "direction": payment.direction,
        "amount": format(payment.amount, "f"),
        "currency_code": case.currency_code,
        "event_timestamp": event_time.isoformat(),
        "counterparty_reference": payment.counterparty,
        "correlation_id": str(uuid5(NAMESPACE_URL, f"amlog-benchmark:{payment.event_id}")),
    }


def _resolve(connection: Any, case: CaseSpec) -> tuple:
    rows = connection.execute(
        """
        SELECT
            alerts.alert_id,
            alerts.recorded_rule_code,
            alerts.triggered_at,
            transactions.transaction_id,
            transactions.source_event_id
        FROM amlog.alerts AS alerts
        JOIN amlog.accounts AS accounts ON accounts.account_id = alerts.account_id
        JOIN amlog.alert_transactions AS links
            ON links.alert_id = alerts.alert_id
           AND links.relationship_role = 'triggering'
        JOIN amlog.transactions AS transactions
            ON transactions.transaction_id = links.transaction_id
        WHERE accounts.account_reference = %s
        """,
        (case.account_reference,),
    ).fetchall()
    if len(rows) != 1:
        raise BenchmarkLinkError(
            f"{case.case_id} produced {len(rows)} rule alerts. "
            "A benchmark case needs exactly one."
        )
    alert_id, rule_code, triggered_at, transaction_id, source_event_id = rows[0]
    if source_event_id != case.subject_event_id or rule_code != case.expected_rule:
        raise BenchmarkLinkError(
            f"{case.case_id} linked to {rule_code} on {source_event_id}. "
            f"The subject payment is {case.subject_event_id} under {case.expected_rule}."
        )
    snapshots = connection.execute(
        """
        SELECT feature_computation_id, input_fingerprint
        FROM amlog.feature_computations
        WHERE alert_id = %s
          AND feature_set_name = %s
          AND feature_set_version = %s
        """,
        (alert_id, FEATURE_SET_NAME, FEATURE_SET_VERSION),
    ).fetchall()
    if len(snapshots) != 1:
        raise BenchmarkLinkError(
            f"{case.case_id} has {len(snapshots)} {FEATURE_SET_NAME} snapshots."
        )
    computation_id, fingerprint = snapshots[0]
    if fingerprint != _usable_fingerprint(connection, alert_id):
        raise BenchmarkLinkError(
            f"{case.case_id} feature snapshot includes activity after the alert "
            "or does not match the stored payments."
        )
    judgment = judge(case.finding)
    return (
        case,
        alert_id,
        computation_id,
        transaction_id,
        rule_code,
        triggered_at,
        fingerprint,
        judgment,
    )


def _usable_fingerprint(connection: Any, alert_id: UUID) -> str:
    alert = _alert(connection, alert_id)
    payments = _payments(connection, alert.account_id)
    if not any(payment.event_time > alert.triggered_at for payment in payments):
        raise BenchmarkLinkError("The account has no payment after the alert.")
    visible = tuple(payment for payment in payments if payment.event_time <= alert.triggered_at)
    with_future = compute_snapshot(alert, payments).input_fingerprint
    without_future = compute_snapshot(alert, visible).input_fingerprint
    if with_future != without_future:
        raise BenchmarkLinkError("A payment after the alert changed the feature fingerprint.")
    return with_future


def _alert(connection: Any, alert_id: UUID) -> AlertView:
    row = connection.execute(
        """
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
            (
                SELECT count(*)::integer
                FROM amlog.alert_transactions AS evidence
                WHERE evidence.alert_id = alerts.alert_id
            ),
            (
                SELECT transaction_id
                FROM amlog.alert_transactions AS evidence
                WHERE evidence.alert_id = alerts.alert_id
                  AND evidence.relationship_role = 'triggering'
            )
        FROM amlog.alerts AS alerts
        JOIN amlog.accounts AS accounts ON accounts.account_id = alerts.account_id
        WHERE alerts.alert_id = %s
        """,
        (alert_id,),
    ).fetchone()
    return AlertView(
        alert_id=row[0],
        account_id=row[1],
        customer_id=row[2],
        triggered_at=row[3],
        window_started_at=row[4],
        window_ended_at=row[5],
        rule_code=row[6],
        rule_version=row[7],
        account_opened_at=row[8],
        account_currency=row[9],
        evidence_count=row[10],
        triggering_transaction_id=row[11],
    )


def _payments(connection: Any, account_id: UUID) -> tuple[PaymentView, ...]:
    rows = connection.execute(
        """
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
        """,
        (account_id,),
    ).fetchall()
    return tuple(
        PaymentView(
            transaction_id=row[0],
            account_id=row[1],
            event_time=row[2],
            direction=row[3],
            amount=row[4],
            currency_code=row[5],
            payment_channel=row[6],
            counterparty_reference=row[7],
        )
        for row in rows
    )


def _insert_links(connection: Any, linked: list[tuple]) -> None:
    cursor = connection.cursor()
    cursor.executemany(
        """
        INSERT INTO amlog_benchmark.alert_links (
            case_id, alert_id, feature_computation_id, triggering_transaction_id,
            recorded_rule_code, triggered_at, input_fingerprint
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (case.case_id, alert_id, computation_id, transaction_id, rule_code, triggered_at, fingerprint)
            for case, alert_id, computation_id, transaction_id, rule_code, triggered_at, fingerprint, _judgment
            in linked
        ],
    )
    cursor.executemany(
        """
        INSERT INTO amlog_benchmark.judgments (
            case_id, outcome, assumption_id, judgment_source, recorded_by, rationale
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            (
                case.case_id,
                judgment.outcome,
                judgment.assumption_id,
                JUDGMENT_SOURCE,
                PROTOCOL_ACTOR,
                judgment.rationale,
            )
            for case, _alert_id, _computation_id, _transaction_id, _rule_code, _triggered_at, _fingerprint, judgment
            in linked
        ],
    )


def _reject_operational_labels(connection: Any) -> None:
    labels = connection.execute("SELECT count(*) FROM amlog_label.alert_labels").fetchone()[0]
    if labels != 0:
        raise BenchmarkLinkError("The benchmark load wrote an operational label.")

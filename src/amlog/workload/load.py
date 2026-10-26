"""Load a fictional workload through the transaction ingestion service.

The target database must be disposable. The development database named by
AMLOG_DATABASE_URL is never dropped, truncated, or written by this module.
Scenario notes are stored in schema amlog_scenario, not on the payment row.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from amlog.config import default_migrations_dir, load_settings
from amlog.workload.generate import (
    ANNOTATION_NOTE,
    GeneratedEvent,
    PartyPlan,
    WorkloadSpec,
    build_parties,
    iter_events,
)

DEFAULT_WORKLOAD_DATABASE = "amlog_workload"
_RESERVED_DATABASES = frozenset({"amlog", "postgres", "template0", "template1"})
_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class WorkloadTargetError(ValueError):
    """The requested database is not a safe disposable workload target."""


@dataclass(frozen=True)
class LoadReport:
    database_name: str
    population_id: str
    seed: int
    event_target: int
    customers: int
    accounts: int
    recorded: int
    already_recorded: int
    rejected: int
    stored_transactions: int
    stored_annotations: int
    generation_seconds: float
    ingestion_seconds: float

    @property
    def events_per_second(self) -> float:
        elapsed = self.generation_seconds + self.ingestion_seconds
        if elapsed <= 0:
            return 0.0
        return self.event_target / elapsed


def assert_workload_database_is_disposable(database_name: str, development_database: str) -> None:
    """Refuse the development database and PostgreSQL's own databases."""

    if not _DATABASE_NAME.match(database_name):
        raise WorkloadTargetError(
            "The workload database name must start with a letter and use only lowercase letters, digits, and underscores."
        )
    if database_name in _RESERVED_DATABASES or database_name == development_database:
        raise WorkloadTargetError(
            f"Refusing to load a workload into database {database_name!r}. "
            "Choose a disposable database that is not the AMLOG development database."
        )


def database_name_from_url(url: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    name = conninfo_to_dict(url).get("dbname") or ""
    return str(name)


def replace_database(url: str, database_name: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = dict(conninfo_to_dict(url))
    info["dbname"] = database_name
    return make_conninfo(**info)


def load_workload(
    development_url: str,
    spec: WorkloadSpec,
    database_name: str = DEFAULT_WORKLOAD_DATABASE,
    *,
    reset: bool = False,
) -> LoadReport:
    """Create or reuse a disposable database, then ingest one population."""

    assert_workload_database_is_disposable(database_name, database_name_from_url(development_url))
    _ensure_database(replace_database(development_url, "postgres"), database_name, reset)
    target = replace_database(development_url, database_name)
    _migrate(target)
    started = datetime.now(timezone.utc)
    generation_started = perf_counter()
    parties = build_parties(spec)
    generation_seconds = perf_counter() - generation_started
    _assert_current_database(target, database_name)
    _store_parties(target, spec.population_id, parties)
    _ensure_annotations(target)
    counts, generation_more, ingestion_seconds = _ingest(target, spec)
    generation_seconds += generation_more
    finished = datetime.now(timezone.utc)
    stored_transactions, stored_annotations = _stored_counts(target, spec.population_id)
    _record_run(
        target,
        spec,
        counts,
        len(parties),
        sum(len(party.accounts) for party in parties),
        generation_seconds,
        ingestion_seconds,
        started,
        finished,
    )
    return LoadReport(
        database_name=database_name,
        population_id=spec.population_id,
        seed=spec.seed,
        event_target=spec.event_count,
        customers=len(parties),
        accounts=sum(len(party.accounts) for party in parties),
        recorded=counts["recorded"],
        already_recorded=counts["already_recorded"],
        rejected=counts["rejected"],
        stored_transactions=stored_transactions,
        stored_annotations=stored_annotations,
        generation_seconds=generation_seconds,
        ingestion_seconds=ingestion_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load a fictional AMLOG payment workload into a disposable database."
    )
    parser.add_argument("--events", type=int, default=2000, help="Number of payment events. No fixed maximum.")
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--population", default="local-validation")
    parser.add_argument("--database", default=DEFAULT_WORKLOAD_DATABASE)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the disposable workload database before loading.",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if settings.database_url is None:
        print(
            "AMLOG_DATABASE_URL is not set. No database was contacted.",
            file=sys.stderr,
        )
        return 2
    try:
        spec = WorkloadSpec(
            seed=args.seed,
            population_id=args.population,
            event_count=args.events,
            batch_size=args.batch_size,
        )
        report = load_workload(settings.database_url, spec, args.database, reset=args.reset)
    except WorkloadTargetError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_report(report)
    return 0 if report.rejected == 0 else 1


def _ensure_database(admin_url: str, database_name: str, reset: bool) -> None:
    import psycopg
    from psycopg import sql

    connection = psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    try:
        if reset:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name))
            )
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        ).fetchone()
        if exists is None:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    finally:
        connection.close()


def _migrate(target_url: str) -> None:
    import psycopg

    from amlog.db.migrate import apply_migrations

    connection = psycopg.connect(target_url, connect_timeout=5)
    try:
        apply_migrations(connection, default_migrations_dir())
    finally:
        connection.close()


def _assert_current_database(target_url: str, database_name: str) -> None:
    import psycopg

    connection = psycopg.connect(target_url, connect_timeout=5)
    try:
        current = connection.execute("SELECT current_database()").fetchone()[0]
    finally:
        connection.close()
    if current != database_name or current in _RESERVED_DATABASES:
        raise WorkloadTargetError(
            f"Connected to {current!r} instead of disposable database {database_name!r}. No parties were written."
        )


def _store_parties(target_url: str, population_id: str, parties: tuple[PartyPlan, ...]) -> None:
    import psycopg

    if not parties:
        return
    connection = psycopg.connect(target_url, autocommit=False, connect_timeout=5)
    try:
        existing = connection.execute(
            "SELECT count(*) FROM amlog.customers WHERE customer_reference LIKE %s",
            (f"WL-{population_id}-C%",),
        ).fetchone()[0]
        if existing == len(parties):
            connection.rollback()
            return
        if existing != 0:
            connection.rollback()
            raise WorkloadTargetError(
                "This population is only partly present in the workload database. "
                "Reload it with --reset, or choose a new population id."
            )
        for party in parties:
            connection.execute(
                """
                INSERT INTO amlog.customers (
                    customer_reference, customer_type, legal_name,
                    residency_country_code, lifecycle_status, onboarded_at
                )
                VALUES (%s, %s, %s, %s, 'active', %s)
                """,
                (
                    party.customer_reference,
                    party.customer_type,
                    party.legal_name,
                    party.residency_country_code,
                    party.onboarded_at,
                ),
            )
            customer_id = connection.execute(
                "SELECT customer_id FROM amlog.customers WHERE customer_reference = %s",
                (party.customer_reference,),
            ).fetchone()[0]
            for account in party.accounts:
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
                        account.account_reference,
                        account.source_system,
                        account.source_account_id,
                        account.account_type,
                        account.currency_code,
                        account.opened_at,
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_annotations(target_url: str) -> None:
    import psycopg
    from psycopg import sql

    connection = psycopg.connect(target_url, autocommit=True, connect_timeout=5)
    try:
        connection.execute("CREATE SCHEMA IF NOT EXISTS amlog_scenario")
        connection.execute(
            sql.SQL("COMMENT ON SCHEMA amlog_scenario IS {}").format(
                sql.Literal(
                    "Development workload notes. These rows are not payment records, "
                    "fraud judgments, or model labels."
                )
            )
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS amlog_scenario.event_annotations (
                  population_id TEXT NOT NULL,
                  source_system TEXT NOT NULL,
                  source_event_id TEXT NOT NULL,
                  scenario_code TEXT NOT NULL,
                  profile_code TEXT NOT NULL,
                  annotation_note TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  CONSTRAINT event_annotations_pkey
                    PRIMARY KEY (population_id, source_system, source_event_id),
                  CONSTRAINT event_annotations_note_chk CHECK (annotation_note = {})
                )
                """
            ).format(sql.Literal(ANNOTATION_NOTE))
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS amlog_scenario.load_runs (
              load_run_id UUID NOT NULL DEFAULT gen_random_uuid(),
              population_id TEXT NOT NULL,
              seed INTEGER NOT NULL,
              event_target INTEGER NOT NULL,
              recorded INTEGER NOT NULL,
              already_recorded INTEGER NOT NULL,
              rejected INTEGER NOT NULL,
              customer_count INTEGER NOT NULL,
              account_count INTEGER NOT NULL,
              generation_seconds NUMERIC NOT NULL,
              ingestion_seconds NUMERIC NOT NULL,
              started_at TIMESTAMPTZ NOT NULL,
              finished_at TIMESTAMPTZ NOT NULL,
              CONSTRAINT load_runs_pkey PRIMARY KEY (load_run_id)
            )
            """
        )
    finally:
        connection.close()


def _ingest(target_url: str, spec: WorkloadSpec) -> tuple[dict[str, int], float, float]:
    import psycopg

    from amlog.ingest.service import TransactionIngestor

    ingest_connection = psycopg.connect(target_url, autocommit=False, connect_timeout=5)
    note_connection = psycopg.connect(target_url, autocommit=False, connect_timeout=5)
    counts = {"recorded": 0, "already_recorded": 0, "rejected": 0}
    generation_seconds = 0.0
    ingestion_seconds = 0.0
    pending: list[tuple[Any, ...]] = []
    try:
        ingestor = TransactionIngestor(ingest_connection)
        stream = iter_events(spec)
        while True:
            started = perf_counter()
            try:
                event = next(stream)
            except StopIteration:
                generation_seconds += perf_counter() - started
                break
            generation_seconds += perf_counter() - started
            started = perf_counter()
            result = ingestor.ingest(event.payload)
            ingestion_seconds += perf_counter() - started
            if result.outcome in counts:
                counts[result.outcome] += 1
            else:
                counts["rejected"] += 1
            if result.stored:
                pending.append(_annotation_row(spec, event))
            if len(pending) >= spec.batch_size:
                _flush_annotations(note_connection, pending)
                note_connection.commit()
                pending.clear()
        if pending:
            _flush_annotations(note_connection, pending)
            note_connection.commit()
    except Exception:
        note_connection.rollback()
        raise
    finally:
        ingest_connection.close()
        note_connection.close()
    return counts, generation_seconds, ingestion_seconds


def _annotation_row(spec: WorkloadSpec, event: GeneratedEvent) -> tuple[Any, ...]:
    return (
        spec.population_id,
        event.payload["source_system"],
        event.payload["source_event_id"],
        event.scenario_code,
        event.profile_code,
        ANNOTATION_NOTE,
    )


def _flush_annotations(connection: Any, rows: Iterable[tuple[Any, ...]]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO amlog_scenario.event_annotations (
                population_id, source_system, source_event_id,
                scenario_code, profile_code, annotation_note
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT event_annotations_pkey DO NOTHING
            """,
            list(rows),
        )


def _stored_counts(target_url: str, population_id: str) -> tuple[int, int]:
    import psycopg

    connection = psycopg.connect(target_url, connect_timeout=5)
    try:
        transactions = connection.execute(
            """
            SELECT count(*)
            FROM amlog.transactions
            WHERE split_part(source_event_id, ':', 2) = %s
            """,
            (population_id,),
        ).fetchone()[0]
        annotations = connection.execute(
            "SELECT count(*) FROM amlog_scenario.event_annotations WHERE population_id = %s",
            (population_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    return int(transactions), int(annotations)


def _record_run(
    target_url: str,
    spec: WorkloadSpec,
    counts: dict[str, int],
    customers: int,
    accounts: int,
    generation_seconds: float,
    ingestion_seconds: float,
    started: datetime,
    finished: datetime,
) -> None:
    import psycopg

    connection = psycopg.connect(target_url, autocommit=True, connect_timeout=5)
    try:
        connection.execute(
            """
            INSERT INTO amlog_scenario.load_runs (
                population_id, seed, event_target, recorded, already_recorded, rejected,
                customer_count, account_count, generation_seconds, ingestion_seconds,
                started_at, finished_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                spec.population_id,
                spec.seed,
                spec.event_count,
                counts["recorded"],
                counts["already_recorded"],
                counts["rejected"],
                customers,
                accounts,
                generation_seconds,
                ingestion_seconds,
                started,
                finished,
            ),
        )
    finally:
        connection.close()


def _print_report(report: LoadReport) -> None:
    print(f"Disposable database: {report.database_name}")
    print(f"Population: {report.population_id}")
    print(f"Seed: {report.seed}")
    print(f"Events requested: {report.event_target}")
    print(f"Customers: {report.customers}")
    print(f"Accounts: {report.accounts}")
    print(f"Recorded: {report.recorded}")
    print(f"Already recorded: {report.already_recorded}")
    print(f"Rejected: {report.rejected}")
    print(f"Transactions stored for this population: {report.stored_transactions}")
    print(f"Scenario annotations stored: {report.stored_annotations}")
    print(f"Generation seconds: {report.generation_seconds:.3f}")
    print(f"Ingestion seconds: {report.ingestion_seconds:.3f}")
    print(f"Events per second: {report.events_per_second:.1f}")
    print("Scenario notes are in amlog_scenario, not on the payment rows.")
    print("This is a fictional development workload, not operational history.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Workload load failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

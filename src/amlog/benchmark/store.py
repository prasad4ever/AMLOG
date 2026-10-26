"""Load the alert-linked benchmark into its own database.

The database name must be ``amlog_benchmark`` or ``amlog_benchmark_`` plus a
short suffix. Operational and workload databases are refused. The operational
schema is applied inside the benchmark database so the existing ingestion,
rule replay, and feature snapshot code can run there. Judgments stay in
schema ``amlog_benchmark`` and are not written to ``amlog_label``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from amlog.benchmark.checks import SpecAssessment, assess_cases
from amlog.benchmark.histories import build_cases
from amlog.benchmark.link import BenchmarkLinkError, load_histories, prepare_benchmark_schema
from amlog.benchmark.phase10a import Phase10AAudit, audit_phase10a
from amlog.benchmark.protocol import DESIGN_ID
from amlog.config import default_migrations_dir
from amlog.db.migrate import apply_migrations
from amlog.workload.load import database_name_from_url, replace_database

_ALLOWED_NAME = re.compile(r"^amlog_benchmark(_[a-z0-9]+)?$")
_PROTECTED = frozenset(
    {
        "amlog",
        "postgres",
        "template0",
        "template1",
        "amlog_workload",
        "amlog_workload_r002_validation",
        "amlog_workload_integrated",
    }
)


class BenchmarkTargetError(ValueError):
    """The requested database is not a disposable benchmark database."""


class BenchmarkCheckError(ValueError):
    """The histories or the loaded rows failed a benchmark check."""


@dataclass(frozen=True)
class BenchmarkLoad:
    """What one benchmark load stored."""

    database_name: str
    design_id: str
    phase10a: Phase10AAudit
    assessment: SpecAssessment
    linked_cases: int
    feature_snapshots: int
    defensible_outcomes: int


def assert_benchmark_database(database_name: str, development_database: str) -> None:
    """Refuse operational, workload, and development databases."""

    if database_name in _PROTECTED or database_name == development_database:
        raise BenchmarkTargetError(
            f"Refusing to load the benchmark into {database_name!r}. "
            "Choose a disposable database whose name starts with amlog_benchmark."
        )
    if _ALLOWED_NAME.fullmatch(database_name) is None:
        raise BenchmarkTargetError(
            f"Refusing to load the benchmark into {database_name!r}. "
            "The database name must be amlog_benchmark or amlog_benchmark_ followed by a short suffix."
        )


def load_benchmark(development_url: str, database_name: str, *, reset: bool) -> BenchmarkLoad:
    """Create the benchmark database, replay the existing rules, and store links."""

    assert_benchmark_database(database_name, database_name_from_url(development_url))
    phase10a = audit_phase10a()
    cases = build_cases()
    assessment = assess_cases(cases)
    if not assessment.passed:
        raise BenchmarkCheckError(" ".join(assessment.failures))
    import psycopg

    admin_url = replace_database(development_url, "postgres")
    _ensure_database(admin_url, database_name, reset)
    target = replace_database(development_url, database_name)
    with psycopg.connect(target, connect_timeout=5) as connection:
        current = connection.execute("SELECT current_database()").fetchone()[0]
        if current != database_name or current in _PROTECTED:
            raise BenchmarkTargetError(
                f"Connected to {current!r} instead of {database_name!r}. No benchmark rows were written."
            )
        apply_migrations(connection, default_migrations_dir())
        try:
            prepare_benchmark_schema(connection)
            load_histories(connection, cases)
            linked, snapshots, outcomes = _counts(connection)
        except BenchmarkLinkError as exc:
            raise BenchmarkCheckError(str(exc)) from exc
        if (linked, snapshots, outcomes) != (len(cases), len(cases), len(cases)):
            raise BenchmarkCheckError(
                f"Linked {linked} cases, {snapshots} snapshots, and {outcomes} outcomes "
                f"from {len(cases)} histories."
            )
    return BenchmarkLoad(
        database_name,
        DESIGN_ID,
        phase10a,
        assessment,
        linked,
        snapshots,
        outcomes,
    )


def _counts(connection) -> tuple[int, int, int]:
    row = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM amlog_benchmark.alert_links),
            (SELECT count(*) FROM amlog.feature_computations),
            (SELECT count(*) FROM amlog_benchmark.judgments)
        """
    ).fetchone()
    return tuple(int(value) for value in row)


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

"""The controlled outcome evaluation must not invent review results.

The decision itself does not need a database. When a database URL is set, a
disposable database proves the command writes nothing.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AMLOG_ENVIRONMENT", "test")

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover
    psycopg = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.config import default_migrations_dir  # noqa: E402
from amlog.db.migrate import apply_migrations  # noqa: E402
from amlog.evaluate.outcomes import (  # noqa: E402
    _COUNT_QUERY,
    _ELIGIBLE_QUERY,
    OutcomeEvidence,
    assess_outcome_evidence,
)
from amlog.evaluate.run import main as evaluate_main  # noqa: E402
from amlog.prioritize.supervised import _LABEL_QUERY  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402

EVALUATE_DATABASE = "amlog_evaluate_test"
PRESERVED = (
    "amlog",
    "amlog_workload",
    "amlog_workload_r002_validation",
    "amlog_workload_integrated",
)


class OutcomeDecisionTests(unittest.TestCase):
    def test_queries_do_not_read_scenario_notes_or_scores_as_outcomes(self) -> None:
        combined = _COUNT_QUERY + _ELIGIBLE_QUERY
        self.assertNotIn("amlog_scenario", combined)
        self.assertNotIn("scenario_code", combined)
        self.assertNotIn("recorded_rule_code", combined)
        self.assertNotIn("risk_scores", _ELIGIBLE_QUERY)
        self.assertNotIn("synthetic_fixture", _LABEL_QUERY)
        self.assertNotIn("synthetic_fixture", _ELIGIBLE_QUERY)

    def test_empty_evidence_blocks_the_comparison_without_a_metric(self) -> None:
        evaluation = assess_outcome_evidence(
            OutcomeEvidence(0, 0, 0, 0, 0, 0, 0, 0, 0)
        )
        text = evaluation.message
        self.assertFalse(evaluation.metrics_computed)
        self.assertIn("BLOCKED", text)
        self.assertIn("Precision at review capacity was not computed.", text)
        self.assertIn("Recall of productive cases was not computed.", text)
        self.assertIn("Missed productive cases were not counted.", text)
        self.assertIn("scenario codes", text)
        self.assertNotIn("ROC-AUC", text)
        self.assertNotIn("%", text)

    def test_a_full_count_floor_still_does_not_emit_a_metric(self) -> None:
        evaluation = assess_outcome_evidence(
            OutcomeEvidence(
                alerts=100,
                investigation_cases=40,
                investigation_notes=40,
                label_rows=80,
                eligible_productive=30,
                eligible_non_productive=30,
                distinct_eligible_reviewers=2,
                model_versions=2,
                risk_scores=200,
            )
        )
        self.assertFalse(evaluation.metrics_computed)
        self.assertIn("does not calculate review-capacity metrics", evaluation.message)
        self.assertNotIn("ROC-AUC", evaluation.message)

    def test_the_development_database_is_refused(self) -> None:
        from io import StringIO
        import contextlib

        errors = StringIO()
        with contextlib.redirect_stderr(errors):
            code = evaluate_main(["--database", "amlog"])
        self.assertEqual(code, 2)
        self.assertIn("not a scoring database", errors.getvalue())


class OutcomeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = {name: _counts(url, name) for name in PRESERVED}
        admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
        try:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(EVALUATE_DATABASE))
            )
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(EVALUATE_DATABASE)))
        finally:
            admin.close()
        with psycopg.connect(replace_database(url, EVALUATE_DATABASE), connect_timeout=5) as connection:
            apply_migrations(connection, default_migrations_dir())

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not os.environ.get("AMLOG_DATABASE_URL", "").strip():
            return
        admin = psycopg.connect(replace_database(cls.url, "postgres"), autocommit=True, connect_timeout=5)
        try:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(EVALUATE_DATABASE))
            )
        finally:
            admin.close()
        after = {name: _counts(cls.url, name) for name in PRESERVED}
        if after != cls.before:
            raise AssertionError(f"A preserved database changed from {cls.before} to {after}.")

    def test_the_command_prints_blocked_and_writes_nothing(self) -> None:
        from io import StringIO
        import contextlib

        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = evaluate_main(["--database", EVALUATE_DATABASE])
        self.assertEqual(code, 3)
        text = buffer.getvalue()
        self.assertIn("BLOCKED", text)
        self.assertIn("was not computed", text)
        self.assertNotIn("ROC-AUC", text)
        with psycopg.connect(replace_database(self.url, EVALUATE_DATABASE), connect_timeout=5) as connection:
            labels, scores, cases = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM amlog_label.alert_labels),
                    (SELECT count(*) FROM amlog.risk_scores),
                    (SELECT count(*) FROM amlog.investigation_cases)
                """
            ).fetchone()
        self.assertEqual((labels, scores, cases), (0, 0, 0))


def _counts(url: str, database_name: str) -> tuple[int, ...] | None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)).fetchone()
    finally:
        admin.close()
    if exists is None:
        return None
    with psycopg.connect(replace_database(url, database_name), connect_timeout=5) as connection:
        return tuple(
            int(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(*table.split(".")))
                ).fetchone()[0]
            )
            for table in (
                "amlog.transactions",
                "amlog.alerts",
                "amlog.investigation_cases",
                "amlog_label.alert_labels",
                "amlog.risk_scores",
            )
        )


if __name__ == "__main__":
    unittest.main()

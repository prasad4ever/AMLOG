"""The benchmark must link to rule-generated alerts without labeling the old dossiers.

Design checks do not need a database. When a database URL is set, a disposable
database proves ingestion, replay, feature snapshots, and the label boundary.
"""

from __future__ import annotations

import inspect
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

from amlog.benchmark.checks import PREDICTIVE_SQL, assess_cases  # noqa: E402
from amlog.benchmark.histories import build_cases  # noqa: E402
from amlog.benchmark.phase10a import audit_phase10a  # noqa: E402
from amlog.benchmark.protocol import (  # noqa: E402
    NON_PRODUCTIVE,
    PRODUCTIVE,
    PROTOCOL_ACTOR,
    ProtocolError,
    SupportFinding,
    judge,
)
from amlog.benchmark.run import main as benchmark_main  # noqa: E402
from amlog.benchmark.store import assert_benchmark_database, load_benchmark  # noqa: E402
from amlog.prioritize.names import (  # noqa: E402
    MIN_SUPERVISED_PER_CLASS,
    MIN_SUPERVISED_REVIEWERS,
)
from amlog.prioritize.supervised import _LABEL_QUERY, assess_supervised_readiness  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402

BENCHMARK_DATABASE = "amlog_benchmark_test"
PRESERVED = (
    "amlog",
    "amlog_workload",
    "amlog_workload_r002_validation",
    "amlog_workload_integrated",
)


class BenchmarkDesignTests(unittest.TestCase):
    def test_phase10a_dossiers_do_not_meet_a_rule_threshold(self) -> None:
        audit = audit_phase10a()
        self.assertEqual(audit.cases, 192)
        self.assertEqual(audit.r001_threshold_met, 0)
        self.assertEqual(audit.r002_threshold_met, 0)

    def test_histories_keep_findings_independent_of_payment_shape(self) -> None:
        assessment = assess_cases()
        self.assertTrue(assessment.passed, assessment.failures)
        self.assertEqual(assessment.case_count, 16)
        self.assertEqual(
            (
                assessment.fit_productive,
                assessment.fit_non_productive,
                assessment.holdout_productive,
                assessment.holdout_non_productive,
            ),
            (4, 4, 4, 4),
        )
        self.assertTrue(any("2 independent cases" in note for note in assessment.notes))

    def test_the_protocol_reads_only_the_support_finding(self) -> None:
        self.assertEqual(list(inspect.signature(judge).parameters), ["finding"])
        absent = SupportFinding("not_produced", True, None, None, None, "No document was produced.")
        matched = SupportFinding("invoice", True, True, True, True, "The invoice matches.")
        mismatched = SupportFinding("invoice", True, True, False, True, "The amount differs.")
        self.assertEqual(judge(absent).outcome, PRODUCTIVE)
        self.assertEqual(judge(matched).outcome, NON_PRODUCTIVE)
        self.assertEqual(judge(mismatched).outcome, PRODUCTIVE)
        self.assertEqual(judge(mismatched).assumption_id, "C1")
        self.assertEqual(judge(matched).assumption_id, "C2")
        with self.assertRaises(ProtocolError):
            judge(SupportFinding("invoice", False, True, True, True, "Dated before the alert."))

    def test_predictive_export_omits_the_judgment_and_the_rule_identity(self) -> None:
        lowered = PREDICTIVE_SQL.lower()
        self.assertNotIn("context_family", lowered)
        self.assertNotIn("document_kind", lowered)
        self.assertNotIn("finding_note", lowered)
        self.assertNotIn("amlog_label", lowered)
        self.assertIn("'rule_code'", PREDICTIVE_SQL)
        self.assertIn("customer_type", PREDICTIVE_SQL)
        self.assertIn("arrangement_on_file", PREDICTIVE_SQL)

    def test_recorded_actor_and_operational_gate_stay_separate(self) -> None:
        self.assertEqual(PROTOCOL_ACTOR, "amlog.benchmark.protocol")
        self.assertNotIn(" ", PROTOCOL_ACTOR)
        self.assertEqual(MIN_SUPERVISED_PER_CLASS, 25)
        self.assertEqual(MIN_SUPERVISED_REVIEWERS, 2)
        self.assertIn("eligible_for_evaluation", _LABEL_QUERY)
        self.assertNotIn("benchmark_protocol", _LABEL_QUERY)
        self.assertNotIn("productive_review", _LABEL_QUERY)
        package = ROOT / "src" / "amlog" / "benchmark"
        text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        self.assertNotIn("sklearn", text)
        self.assertNotIn("train_supervised", text)
        self.assertNotRegex(text, r"eligible_for_evaluation\s*=")
        self.assertNotIn("INSERT INTO amlog_label", text)

    def test_protected_databases_are_refused(self) -> None:
        for name in (
            "amlog",
            "amlog_workload",
            "amlog_workload_r002_validation",
            "amlog_workload_integrated",
            "postgres",
        ):
            with self.assertRaises(ValueError):
                assert_benchmark_database(name, "amlog")

    def test_case_count_is_the_specified_history_not_a_balanced_filler_set(self) -> None:
        cases = build_cases()
        self.assertEqual(len({case.case_id for case in cases}), 16)
        self.assertLess(
            sum(1 for case in cases if judge(case.finding).outcome == PRODUCTIVE),
            MIN_SUPERVISED_PER_CLASS,
        )


class BenchmarkStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = {name: _counts(url, name) for name in PRESERVED}

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not os.environ.get("AMLOG_DATABASE_URL", "").strip():
            return
        admin = psycopg.connect(replace_database(cls.url, "postgres"), autocommit=True, connect_timeout=5)
        try:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(BENCHMARK_DATABASE)
                )
            )
        finally:
            admin.close()
        after = {name: _counts(cls.url, name) for name in PRESERVED}
        if after != cls.before:
            raise AssertionError(f"A preserved database changed from {cls.before} to {after}.")

    def test_histories_link_to_alerts_and_snapshots(self) -> None:
        from io import StringIO
        import contextlib

        loaded = load_benchmark(self.url, BENCHMARK_DATABASE, reset=True)
        self.assertEqual(loaded.phase10a.cases, 192)
        self.assertEqual(loaded.phase10a.r001_threshold_met, 0)
        self.assertEqual(loaded.linked_cases, 16)
        self.assertEqual(loaded.feature_snapshots, 16)
        self.assertEqual(loaded.defensible_outcomes, 16)
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = benchmark_main(["--database", BENCHMARK_DATABASE, "--reset"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("Phase 10A dossiers assessed: 192", text)
        self.assertIn("Phase 10A dossiers meeting an R001 or R002 threshold: 0", text)
        self.assertIn("Linked to a rule-generated alert: 16", text)
        self.assertIn("Usable alert-level feature snapshots: 16", text)
        self.assertIn("Defensible constructed outcomes: 16", text)
        self.assertIn("No model was trained.", text)
        self.assertIn("Prediction accuracy was not computed.", text)
        self.assertIn("not ready", text)
        self.assertNotIn("ROC-AUC", text)
        with psycopg.connect(replace_database(self.url, BENCHMARK_DATABASE), connect_timeout=5) as connection:
            alerts, labels, judgments, links, scenarios = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM amlog.alerts),
                    (SELECT count(*) FROM amlog_label.alert_labels),
                    (SELECT count(*) FROM amlog_benchmark.judgments),
                    (SELECT count(*) FROM amlog_benchmark.alert_links),
                    to_regclass('amlog_scenario.event_annotations')
                """
            ).fetchone()
            readiness = assess_supervised_readiness(connection)
            rule_rows = connection.execute(
                """
                SELECT count(*)
                FROM amlog.alert_features
                WHERE feature_name = 'rule_code'
                """
            ).fetchone()[0]
            exported = connection.execute(PREDICTIVE_SQL).fetchall()
        self.assertEqual((alerts, labels, judgments, links), (16, 0, 16, 16))
        self.assertIsNone(scenarios)
        self.assertTrue(readiness.blocked)
        self.assertEqual(readiness.eligible_productive, 0)
        self.assertEqual(readiness.eligible_non_productive, 0)
        self.assertGreater(rule_rows, 0)
        self.assertTrue(exported)
        self.assertTrue(all(row[0] != "rule_code" for row in exported))


def _counts(url: str, database_name: str) -> tuple[int, ...] | None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        ).fetchone()
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

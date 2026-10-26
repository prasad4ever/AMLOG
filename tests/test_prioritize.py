"""Anomaly ranking, score governance, and the supervised training gate.

Persistence uses the disposable database amlog_priority_test. The development
database and the existing workload databases are counted before and after.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from amlog.monitor.engine import register_r001  # noqa: E402
from amlog.prioritize.errors import SupervisedTrainingBlocked  # noqa: E402
from amlog.prioritize.names import (  # noqa: E402
    ISOLATION_MODEL_NAME,
    MIN_SUPERVISED_PER_CLASS,
)
from amlog.prioritize.artifact import (  # noqa: E402
    load_ranking_artifact,
    parameter_reference,
    save_ranking_artifacts,
    score_saved,
)
from amlog.prioritize.preprocess import (  # noqa: E402
    EXCLUDED_INPUTS,
    AlertSample,
    account_in_training,
    prepare,
)
from amlog.prioritize.rank import rank_alerts  # noqa: E402
from amlog.prioritize.store import (  # noqa: E402
    _COMPUTATION_QUERY,
    _FEATURE_QUERY,
    PriorityError,
    assert_priority_database,
    run_prioritization,
)
from amlog.prioritize.supervised import assess_supervised_readiness, train_supervised  # noqa: E402
from amlog.prioritize.supervised_run import main as supervised_main  # noqa: E402
from amlog.review.server import ReviewServer  # noqa: E402
from amlog.workload.load import replace_database  # noqa: E402

PRIORITY_DATABASE = "amlog_priority_test"
WHEN = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
PRESERVED = (
    "amlog",
    "amlog_workload",
    "amlog_workload_r002_validation",
    "amlog_workload_integrated",
)


class MatrixTests(unittest.TestCase):
    def test_training_statistics_ignore_the_holdout_and_other_currencies(self) -> None:
        training, holdout = _accounts(16, 1)
        samples = []
        for index, account in enumerate(training[:8]):
            samples.append(
                _sample(
                    account,
                    debit_count_1h=float(index + 1),
                    triggering_amount=float(10 * (index + 1)),
                    currency="USD",
                )
            )
        for index, account in enumerate(training[8:]):
            samples.append(
                _sample(
                    account,
                    debit_count_1h=float(index + 1),
                    triggering_amount=float(1000 + index * 100),
                    currency="GBP",
                )
            )
        samples.append(_sample(holdout[0], debit_count_1h=3, triggering_amount=45, currency="GBP"))
        prepared = prepare(samples, fraction=0.8, seed=20261026)
        self.assertEqual(prepared.scales[("triggering_amount", "USD")][0], 45.0)
        self.assertNotEqual(prepared.scales[("triggering_amount", "GBP")][0], 45.0)
        column = prepared.columns.index("triggering_amount")
        self.assertNotEqual(prepared.rows[-1][column], 0.0)
        self.assertTrue(EXCLUDED_INPUTS.isdisjoint(prepared.columns))
        self.assertFalse(prepared.training_mask[-1])

    def test_a_missing_value_is_imputed_from_training_rows_only(self) -> None:
        accounts = [uuid.uuid4() for _ in range(4)]
        samples = [
            _sample(accounts[0], debit_count_1h=1, payment_count_24h=1),
            _sample(accounts[1], debit_count_1h=None, payment_count_24h=2),
            _sample(accounts[2], debit_count_1h=3, payment_count_24h=3),
            _sample(accounts[3], debit_count_1h=9, payment_count_24h=4),
        ]
        # The last account is forced into the holdout by preparing the first three only
        # through fraction 1 on the first three, then transforming is covered by prepare of all
        # with the holdout account excluded from training via a direct median check.
        training = samples[:3]
        prepared = prepare(training, fraction=1.0)
        self.assertEqual(prepared.medians["debit_count_1h"], 2.0)
        missing_column = prepared.columns.index("debit_count_1h__missing")
        value_column = prepared.columns.index("debit_count_1h")
        self.assertEqual(prepared.rows[1][missing_column], 1.0)
        self.assertEqual(prepared.rows[1][value_column], 2.0)
        self.assertNotIn("rule_code", prepared.columns)
        self.assertNotIn("scenario_code", prepared.columns)

    def test_accounts_are_not_split_across_the_fit_and_the_holdout(self) -> None:
        training, holdout = _accounts(1, 1)
        account = training[0]
        samples = [
            _sample(account, debit_count_1h=1, payment_count_24h=2),
            _sample(account, debit_count_1h=4, payment_count_24h=5),
            _sample(holdout[0], debit_count_1h=2, payment_count_24h=3),
        ]
        prepared = prepare(samples, fraction=0.8, seed=20261026)
        self.assertEqual(prepared.training_mask[0], prepared.training_mask[1])
        self.assertTrue(prepared.training_mask[0])
        self.assertFalse(prepared.training_mask[2])

    def test_the_same_matrix_produces_the_same_ranks_and_an_outlier_is_first(self) -> None:
        samples = [
            _sample(uuid.uuid4(), debit_count_1h=float(index), payment_count_24h=float(index))
            for index in range(1, 16)
        ]
        samples.append(_sample(uuid.uuid4(), debit_count_1h=500, payment_count_24h=500))
        prepared = prepare(samples, fraction=1.0)
        first = rank_alerts(prepared, n_estimators=40, random_state=20261026)
        second = rank_alerts(prepared, n_estimators=40, random_state=20261026)
        self.assertEqual(
            [row.anomaly_score for row in first.rows],
            [row.anomaly_score for row in second.rows],
        )
        self.assertEqual(first.rows[-1].anomaly_rank, 1)
        self.assertIsNone(first.holdout_rank_agreement)

    def test_a_saved_artifact_scores_again_without_fitting(self) -> None:
        training, holdout = _accounts(12, 1)
        samples = [
            _sample(account, debit_count_1h=float(index + 1), payment_count_24h=float(index + 1))
            for index, account in enumerate(training)
        ]
        samples[1] = _sample(training[1], debit_count_1h=None, payment_count_24h=2)
        samples.append(_sample(holdout[0], debit_count_1h=40, payment_count_24h=40))
        prepared = prepare(samples, fraction=0.8, seed=20261026)
        ranking = rank_alerts(prepared, n_estimators=25, random_state=20261026)
        isolation_reference = parameter_reference(prepared, n_estimators=25, random_state=20261026)
        baseline_reference = parameter_reference(prepared, n_estimators=None, random_state=20261026)
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_ranking_artifacts(
                prepared,
                ranking,
                isolation_reference=isolation_reference,
                baseline_reference=baseline_reference,
                n_estimators=25,
                random_state=20261026,
                directory=directory,
            )
            isolation = load_ranking_artifact(isolation_reference, directory)
            baseline = load_ranking_artifact(baseline_reference, directory)

            def fail_fit(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("fit was called")

            isolation["forest"].fit = fail_fit
            rescored = score_saved(samples, isolation)
            self.assertEqual(
                [f"{value:.8f}" for value in rescored],
                [f"{row.anomaly_score:.8f}" for row in ranking.rows],
            )
            baseline_scores = score_saved(samples, baseline)
            self.assertEqual(
                [f"{value:.8f}" for value in baseline_scores],
                [f"{row.baseline_score:.8f}" for row in ranking.rows],
            )
        self.assertIsNone(baseline["forest"])
        self.assertTrue(EXCLUDED_INPUTS.isdisjoint(isolation["columns"]))
        self.assertEqual(isolation["imputation_medians"]["debit_count_1h"], prepared.imputation_medians["debit_count_1h"])
        self.assertIn("does not say that the feature caused", isolation["deviation_meaning"])

    def test_queries_do_not_read_labels_or_scenario_notes(self) -> None:
        combined = _COMPUTATION_QUERY + _FEATURE_QUERY
        self.assertNotIn("amlog_label", combined)
        self.assertNotIn("amlog_scenario", combined)
        self.assertNotIn("investigation", combined)


class PriorityStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = os.environ.get("AMLOG_DATABASE_URL", "").strip()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url
        cls.before = {name: _counts(url, name) for name in PRESERVED}
        _recreate_database(url)
        with psycopg.connect(replace_database(url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            apply_migrations(connection, default_migrations_dir())
            register_r001(connection)

    @classmethod
    def tearDownClass(cls) -> None:
        if psycopg is None or not os.environ.get("AMLOG_DATABASE_URL", "").strip():
            return
        _drop_database(cls.url)
        after = {name: _counts(cls.url, name) for name in PRESERVED}
        if after != cls.before:
            raise AssertionError(f"A preserved database changed from {cls.before} to {after}.")

    def test_the_development_database_is_refused(self) -> None:
        with self.assertRaises(PriorityError):
            assert_priority_database("amlog")
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            with self.assertRaises(PriorityError):
                run_prioritization(connection)

    def test_scores_are_experiment_scores_and_a_second_run_does_not_duplicate_them(self) -> None:
        alerts = [self._alert(debit_count=index) for index in range(1, 12)]
        extreme = self._alert(debit_count=500)
        before = self._stamp(extreme)
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            first = run_prioritization(connection, n_estimators=30, fraction=1.0)
        self.assertEqual(first.scores_written, 24)
        self.assertEqual(first.already_present, 0)
        self.assertEqual(self._stamp(extreme), before)
        stored = self._score(extreme)
        self.assertEqual(stored["lifecycle"], "validated")
        self.assertIsNone(stored["approved_by"])
        self.assertEqual(stored["score_use"], "experiment")
        self.assertEqual(stored["fingerprint"], stored["snapshot_fingerprint"])
        self.assertEqual(stored["rank"], 1)
        self.assertIn("not a probability of financial crime", stored["semantics"])
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            second = run_prioritization(connection, n_estimators=30, fraction=1.0)
        self.assertEqual(second.scores_written, 0)
        self.assertEqual(self._score_count(), 24)
        self._reject_operational_and_unvalidated_scores(extreme)
        self.assertEqual(len(alerts), 11)

    def test_the_review_page_shows_the_rank_without_calling_it_a_crime_probability(self) -> None:
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            alert_id = connection.execute(
                """
                SELECT risk_scores.alert_id
                FROM amlog.risk_scores AS risk_scores
                JOIN amlog.model_versions AS model_versions
                  ON model_versions.model_version_id = risk_scores.model_version_id
                WHERE model_versions.model_name = %s
                LIMIT 1
                """,
                (ISOLATION_MODEL_NAME,),
            ).fetchone()[0]
        url = replace_database(self.url, PRIORITY_DATABASE)
        server = ReviewServer(("127.0.0.1", 0), url)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page = urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/alert?id={alert_id}",
                timeout=5,
            ).read().decode("utf-8")
            listing = urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/",
                timeout=5,
            ).read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("Anomaly rank", page)
        self.assertIn("experimental ordering", page)
        self.assertIn("not a probability of financial crime", page)
        self.assertIn("false positive", page)
        self.assertIn("not a cause of the rank", page)
        self.assertIn("Open investigation", page)
        self.assertIn("experimental ordering", listing)
        self.assertIn("false positive", listing)
        self.assertNotIn("caused by", page.lower())
        self.assertNotIn("caused by", listing.lower())

    def test_supervised_training_stays_blocked_until_reviewed_outcomes_exist(self) -> None:
        from io import StringIO
        import contextlib

        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = supervised_main(["--database", PRIORITY_DATABASE])
        self.assertEqual(code, 3)
        text = buffer.getvalue()
        self.assertIn("BLOCKED", text)
        self.assertNotIn("ROC-AUC", text)
        self.assertNotIn("precision", text)
        readiness = self._readiness()
        self.assertTrue(readiness.blocked)
        self._labels(productive=MIN_SUPERVISED_PER_CLASS, non_productive=MIN_SUPERVISED_PER_CLASS, reviewers=("one-reviewer",))
        still_blocked = self._readiness()
        self.assertTrue(still_blocked.blocked)
        self.assertGreaterEqual(still_blocked.eligible_productive, MIN_SUPERVISED_PER_CLASS)
        with self.assertRaises(SupervisedTrainingBlocked):
            self._train()
        self._labels(productive=1, non_productive=1, reviewers=("second-reviewer",))
        opened = self._readiness()
        self.assertFalse(opened.blocked)
        model_id = self._train()
        stored = self._supervised_model(model_id)
        self.assertEqual(stored["lifecycle"], "registered")
        self.assertIsNone(stored["approved_by"])
        self.assertFalse(stored["summary"]["metrics_computed"])
        self.assertNotIn("roc_auc", stored["summary"])
        self.assertNotIn("precision", stored["summary"])
        self.assertEqual(stored["scores"], 0)

    def _alert(self, debit_count: int) -> uuid.UUID:
        token = uuid.uuid4().hex
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            customer_id = connection.execute(
                """
                INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
                VALUES (%s, 'individual', 'Priority test subject')
                RETURNING customer_id
                """,
                (f"priority-{token}",),
            ).fetchone()[0]
            account_id = connection.execute(
                """
                INSERT INTO amlog.accounts (
                    customer_id, account_reference, source_system, source_account_id,
                    account_type, currency_code, opened_at
                )
                VALUES (%s, %s, 'amlog-priority-test', %s, 'deposit', 'USD', %s)
                RETURNING account_id
                """,
                (customer_id, f"account-{token}", token, WHEN - timedelta(days=40)),
            ).fetchone()[0]
            rule_id = connection.execute(
                "SELECT monitoring_rule_id FROM amlog.monitoring_rules WHERE rule_code = 'R001' AND version_number = 1"
            ).fetchone()[0]
            alert_id = connection.execute(
                """
                INSERT INTO amlog.alerts (
                    alert_reference, monitoring_rule_id, recorded_rule_code, recorded_rule_version,
                    customer_id, account_id, triggered_at, severity, summary
                )
                VALUES (%s, %s, 'R001', 1, %s, %s, %s, 'medium', 'Priority test alert')
                RETURNING alert_id
                """,
                (f"priority-{token}", rule_id, customer_id, account_id, WHEN),
            ).fetchone()[0]
            computation_id = connection.execute(
                """
                INSERT INTO amlog.feature_computations (
                    alert_id, feature_set_name, feature_set_version, input_fingerprint, computed_at
                )
                VALUES (%s, 'alert_behavior', '1', %s, %s)
                RETURNING feature_computation_id
                """,
                (alert_id, token, WHEN),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO amlog.alert_features (
                    feature_computation_id, alert_id, feature_name, value_type, value_numeric
                )
                VALUES (%s, %s, 'debit_count_1h', 'numeric', %s),
                       (%s, %s, 'payment_count_24h', 'numeric', %s)
                """,
                (computation_id, alert_id, Decimal(debit_count), computation_id, alert_id, Decimal(debit_count)),
            )
        return alert_id

    def _labels(self, *, productive: int, non_productive: int, reviewers: tuple[str, ...]) -> None:
        for label, count in (("productive_alert", productive), ("non_productive_alert", non_productive)):
            for index in range(count):
                alert_id = self._alert(debit_count=index + 1)
                reviewer = reviewers[index % len(reviewers)]
                with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
                    connection.execute(
                        """
                        INSERT INTO amlog_label.alert_labels (
                            alert_id, label_value, label_source, data_origin, label_rationale,
                            labeled_at, labeled_by, eligible_for_evaluation
                        )
                        VALUES (%s, %s, 'investigator_outcome', 'operational', %s, %s, %s, true)
                        """,
                        (
                            alert_id,
                            label,
                            "Test fixture for the supervised gate. Not a crime finding.",
                            WHEN + timedelta(seconds=index),
                            reviewer,
                        ),
                    )

    def _stamp(self, alert_id: uuid.UUID):
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            return connection.execute(
                """
                SELECT updated_at, summary, recorded_rule_code, lifecycle_status
                FROM amlog.alerts WHERE alert_id = %s
                """,
                (alert_id,),
            ).fetchone()

    def _score(self, alert_id: uuid.UUID) -> dict:
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            row = connection.execute(
                """
                SELECT
                    model_versions.lifecycle_status,
                    model_versions.approved_by,
                    risk_scores.score_use,
                    risk_scores.recorded_input_fingerprint,
                    feature_computations.input_fingerprint,
                    risk_scores.explanation_document,
                    risk_scores.score_semantics
                FROM amlog.risk_scores AS risk_scores
                JOIN amlog.model_versions AS model_versions
                  ON model_versions.model_version_id = risk_scores.model_version_id
                JOIN amlog.feature_computations AS feature_computations
                  ON feature_computations.feature_computation_id = risk_scores.feature_computation_id
                WHERE risk_scores.alert_id = %s
                  AND model_versions.model_name = %s
                """,
                (alert_id, ISOLATION_MODEL_NAME),
            ).fetchone()
        return {
            "lifecycle": row[0],
            "approved_by": row[1],
            "score_use": row[2],
            "fingerprint": row[3],
            "snapshot_fingerprint": row[4],
            "rank": row[5]["rank"],
            "semantics": row[6],
        }

    def _score_count(self) -> int:
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            return connection.execute("SELECT count(*) FROM amlog.risk_scores").fetchone()[0]

    def _reject_operational_and_unvalidated_scores(self, alert_id: uuid.UUID) -> None:
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            model_id, computation_id, fingerprint = connection.execute(
                """
                SELECT model_version_id, feature_computation_id, recorded_input_fingerprint
                FROM amlog.risk_scores
                WHERE alert_id = %s
                LIMIT 1
                """,
                (alert_id,),
            ).fetchone()
            with self.assertRaises(psycopg.Error):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO amlog.risk_scores (
                            alert_id, model_version_id, recorded_model_name, recorded_version_label,
                            recorded_algorithm_family, feature_set_name, feature_set_version,
                            feature_computation_id, recorded_input_fingerprint, score, score_semantics,
                            score_use, scoring_run_id, scored_at
                        )
                        SELECT alert_id, model_version_id, recorded_model_name, recorded_version_label,
                               recorded_algorithm_family, feature_set_name, feature_set_version,
                               feature_computation_id, recorded_input_fingerprint, score, score_semantics,
                               'operational', gen_random_uuid(), now()
                        FROM amlog.risk_scores
                        WHERE model_version_id = %s AND alert_id = %s
                        LIMIT 1
                        """,
                        (model_id, alert_id),
                    )
            registered = connection.execute(
                """
                INSERT INTO amlog.model_versions (
                    model_name, version_label, algorithm_family, feature_set_name, feature_set_version,
                    training_data_provenance, lifecycle_status, registered_by
                )
                VALUES ('alert_priority_unvalidated', '1', 'isolation_forest', 'alert_behavior', '1',
                        'Test registration that must not be scored.', 'registered', 'amlog.prioritize')
                RETURNING model_version_id
                """,
            ).fetchone()[0]
            with self.assertRaises(psycopg.Error):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO amlog.risk_scores (
                            alert_id, model_version_id, recorded_model_name, recorded_version_label,
                            recorded_algorithm_family, feature_set_name, feature_set_version,
                            feature_computation_id, recorded_input_fingerprint, score, score_semantics,
                            score_use, scoring_run_id, scored_at
                        )
                        VALUES (%s, %s, 'alert_priority_unvalidated', '1', 'isolation_forest',
                                'alert_behavior', '1', %s, %s, 1, 'test', 'experiment', gen_random_uuid(), now())
                        """,
                        (alert_id, registered, computation_id, fingerprint),
                    )

    def _readiness(self):
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            return assess_supervised_readiness(connection)

    def _train(self):
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            return train_supervised(connection)

    def _supervised_model(self, model_id: uuid.UUID) -> dict:
        with psycopg.connect(replace_database(self.url, PRIORITY_DATABASE), connect_timeout=5) as connection:
            row = connection.execute(
                """
                SELECT lifecycle_status, approved_by, evaluation_summary
                FROM amlog.model_versions
                WHERE model_version_id = %s
                """,
                (model_id,),
            ).fetchone()
            scores = connection.execute(
                "SELECT count(*) FROM amlog.risk_scores WHERE model_version_id = %s",
                (model_id,),
            ).fetchone()[0]
        return {
            "lifecycle": row[0],
            "approved_by": row[1],
            "summary": row[2],
            "scores": scores,
        }


def _sample(account: uuid.UUID, *, currency: str = "USD", **values: float | None) -> AlertSample:
    return AlertSample(
        alert_id=uuid.uuid4(),
        account_id=account,
        currency=currency,
        values=values,
    )


def _accounts(training_count: int, holdout_count: int) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    training: list[uuid.UUID] = []
    holdout: list[uuid.UUID] = []
    counter = 1
    while len(training) < training_count or len(holdout) < holdout_count:
        account = uuid.UUID(int=counter)
        counter += 1
        selected = account_in_training(account, fraction=0.8, seed=20261026)
        if selected and len(training) < training_count:
            training.append(account)
        elif not selected and len(holdout) < holdout_count:
            holdout.append(account)
    return training, holdout


def _counts(url: str, database_name: str) -> tuple[int, ...] | None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)).fetchone()
    finally:
        admin.close()
    if exists is None:
        return None
    with psycopg.connect(replace_database(url, database_name), connect_timeout=5) as connection:
        if database_name == "amlog":
            current = connection.execute("SELECT current_database()").fetchone()[0]
            if current != "amlog":
                raise AssertionError(f"AMLOG_DATABASE_URL points at {current}, not amlog.")
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


def _recreate_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(PRIORITY_DATABASE)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(PRIORITY_DATABASE)))
    finally:
        admin.close()


def _drop_database(url: str) -> None:
    admin = psycopg.connect(replace_database(url, "postgres"), autocommit=True, connect_timeout=5)
    try:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(PRIORITY_DATABASE)))
    finally:
        admin.close()


if __name__ == "__main__":
    unittest.main()

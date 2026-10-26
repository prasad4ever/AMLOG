"""SynthAML research benchmark: features, split, scaling, rank, and metrics.

These tests use small CSV fixtures. They do not read the 16-million-row file
and they do not open a database.
"""

from __future__ import annotations

import inspect
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from amlog.synthaml.dataset import assert_outside_repository, file_sha256, load_alert_dates
from amlog.synthaml.errors import SynthamlError, SynthamlLeakageError
from amlog.synthaml.experiment import assert_published_population, format_report, run_experiment
from amlog.synthaml.experiment import PopulationCounts
from amlog.synthaml.features import build_feature_matrix
from amlog.synthaml.metrics import (
    chronological_order,
    compare_capacities,
    measure,
    model_order,
    review_capacity,
)
from amlog.synthaml.model import (
    fit_report_model,
    load_model_bundle,
    save_model_bundle,
    score_report_model,
    score_saved_bundle,
)
from amlog.synthaml.names import (
    CAPACITIES,
    FEATURE_NAMES,
    MISSING_GROUP_SENTINEL,
    PUBLISHED_ALERTS,
    PUBLISHED_DISMISSALS,
    PUBLISHED_REPORTS,
    PUBLISHED_TEST_ALERTS,
    PUBLISHED_TEST_REPORTS,
    PUBLISHED_TRAIN_ALERTS,
    PUBLISHED_TRAIN_REPORTS,
    PUBLISHED_TRANSACTION_ROWS,
)
from amlog.synthaml.preprocess import apply_training_scale, fit_training_scale

REPO = Path(__file__).resolve().parents[1]


def _alert_csv(path: Path, rows: list[tuple[int, str, str]]) -> None:
    lines = ["AlertID,Date,Outcome"]
    lines.extend(f"{alert_id},{alert_day},{outcome}" for alert_id, alert_day, outcome in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _transaction_csv(path: Path, rows: list[tuple[int, str, str, str, float]]) -> None:
    lines = ["AlertID,Timestamp,Entry,Type,Size"]
    lines.extend(
        f"{alert_id},{timestamp},{entry},{transaction_type},{size:.1f}"
        for alert_id, timestamp, entry, transaction_type, size in rows
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FeatureTests(unittest.TestCase):
    def test_feature_names_are_the_published_56(self) -> None:
        self.assertEqual(len(FEATURE_NAMES), 56)
        self.assertEqual(len(set(FEATURE_NAMES)), 56)
        self.assertEqual(FEATURE_NAMES[0], "minimum_Credit_Card")
        self.assertEqual(FEATURE_NAMES[5], "count_Credit_Card")
        self.assertEqual(FEATURE_NAMES[6], "sum_Credit_Card")
        for name in FEATURE_NAMES:
            lowered = name.lower()
            self.assertNotIn("outcome", lowered)
            self.assertNotIn("report", lowered)
            self.assertNotIn("dismiss", lowered)

    def test_builder_does_not_accept_an_outcome(self) -> None:
        self.assertNotIn("outcome", inspect.signature(build_feature_matrix).parameters)
        source = (REPO / "src" / "amlog" / "synthaml" / "features.py").read_text(encoding="utf-8")
        self.assertNotIn("Report", source)
        self.assertNotIn("Dismiss", source)

    def test_statistics_and_future_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alerts = root / "alerts.csv"
            transactions = root / "transactions.csv"
            _alert_csv(alerts, [(7, "2020-06-01", "Report"), (8, "2020-06-01", "Dismiss")])
            _transaction_csv(
                transactions,
                [
                    (7, "2020-06-01 00:00:01", "Credit", "Card", 1.0),
                    (7, "2020-06-01 00:00:02", "Credit", "Card", 3.0),
                    (7, "2020-06-02 00:00:03", "Credit", "Card", 100.0),
                    (7, "2020-06-01 00:00:04", "Debit", "Wire", 1.0),
                    (7, "2020-06-01 00:00:05", "Debit", "Wire", 2.0),
                    (7, "2020-06-01 00:00:06", "Debit", "Wire", 3.0),
                    (7, "2020-06-01 00:00:07", "Debit", "Wire", 4.0),
                    (7, "2020-06-01 23:59:59", "Credit", "Cash", 5.0),
                    (8, "2019-01-01 00:00:01", "Credit", "Card", 9.0),
                ],
            )
            built = build_feature_matrix(load_alert_dates(alerts), transactions)
        self.assertEqual(built.future_rows, 1)
        self.assertEqual(built.transaction_rows, 9)
        self.assertEqual(built.rows_used, 8)
        self.assertEqual(built.older_than_365_rows, 1)
        self.assertEqual(built.alerts_without_transactions, 0)
        row = built.matrix[built.alert_ids.index(7)]
        card = row[0:7]
        self.assertAlmostEqual(card[0], 1.0)
        self.assertAlmostEqual(card[1], 2.0)
        self.assertAlmostEqual(card[2], 2.0)
        self.assertAlmostEqual(card[3], 3.0)
        self.assertAlmostEqual(card[4], 1.0)
        self.assertEqual(card[5], 2.0)
        self.assertAlmostEqual(card[6], 4.0)
        cash = row[7:14]
        self.assertEqual(cash[5], 1.0)
        self.assertAlmostEqual(cash[4], 0.0)
        self.assertAlmostEqual(cash[6], 5.0)
        debit_card = row[28:35]
        self.assertEqual(list(debit_card), [MISSING_GROUP_SENTINEL] * 5 + [0.0, MISSING_GROUP_SENTINEL])
        wire = row[49:56]
        self.assertAlmostEqual(wire[2], 2.5)
        self.assertAlmostEqual(wire[4], math.sqrt(1.25))
        self.assertAlmostEqual(wire[6], 10.0)
        older = built.matrix[built.alert_ids.index(8)]
        self.assertAlmostEqual(older[6], 9.0)

    def test_outcome_text_does_not_change_the_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.csv"
            second = root / "b.csv"
            transactions = root / "t.csv"
            _alert_csv(first, [(1, "2020-01-02", "Report")])
            _alert_csv(second, [(1, "2020-01-02", "Dismiss")])
            _transaction_csv(transactions, [(1, "2020-01-02 00:00:01", "Credit", "Card", 1.5)])
            left = build_feature_matrix(load_alert_dates(first), transactions)
            right = build_feature_matrix(load_alert_dates(second), transactions)
            self.assertEqual(left.transaction_sha256, file_sha256(transactions))
        self.assertEqual(left.fingerprint, right.fingerprint)

    def test_unsorted_file_matches_grouped_file(self) -> None:
        rows = [
            (1, "2020-04-01 00:00:01", "Credit", "Card", 1.0),
            (1, "2020-04-01 00:00:02", "Debit", "Cash", 4.0),
            (2, "2020-04-02 00:00:01", "Credit", "Wire", 2.0),
            (2, "2020-04-02 00:00:02", "Credit", "Wire", 6.0),
        ]
        shuffled = [rows[2], rows[0], rows[3], rows[1]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alerts = root / "alerts.csv"
            grouped = root / "grouped.csv"
            mixed = root / "mixed.csv"
            _alert_csv(
                alerts,
                [(1, "2020-04-01", "Dismiss"), (2, "2020-04-02", "Report")],
            )
            _transaction_csv(grouped, rows)
            _transaction_csv(mixed, shuffled)
            dates = load_alert_dates(alerts)
            left = build_feature_matrix(dates, grouped)
            right = build_feature_matrix(dates, mixed)
            self.assertEqual(left.transaction_sha256, file_sha256(grouped))
        self.assertTrue(left.grouped_read)
        self.assertFalse(right.grouped_read)
        np.testing.assert_allclose(left.matrix, right.matrix)
        self.assertEqual(left.alert_ids, right.alert_ids)


class SplitAndScaleTests(unittest.TestCase):
    def test_capacities_and_published_counts_are_fixed(self) -> None:
        self.assertEqual(CAPACITIES, ((5, 100), (10, 100), (20, 100)))
        self.assertEqual(review_capacity(PUBLISHED_TEST_ALERTS, 5, 100), 458)
        self.assertEqual(review_capacity(PUBLISHED_TEST_ALERTS, 10, 100), 916)
        self.assertEqual(review_capacity(PUBLISHED_TEST_ALERTS, 20, 100), 1832)
        with self.assertRaises(SynthamlError):
            review_capacity(PUBLISHED_TEST_ALERTS, 15, 100)

    def test_published_population_check(self) -> None:
        counts = PopulationCounts(
            alerts=PUBLISHED_ALERTS,
            train_alerts=PUBLISHED_TRAIN_ALERTS,
            test_alerts=PUBLISHED_TEST_ALERTS,
            reports=PUBLISHED_REPORTS,
            dismissals=PUBLISHED_DISMISSALS,
            train_reports=PUBLISHED_TRAIN_REPORTS,
            test_reports=PUBLISHED_TEST_REPORTS,
            transaction_rows=PUBLISHED_TRANSACTION_ROWS,
            future_rows=0,
            older_than_365_rows=0,
            alerts_without_transactions=0,
        )
        assert_published_population(counts)
        with self.assertRaises(SynthamlError):
            assert_published_population(
                PopulationCounts(
                    alerts=counts.alerts,
                    train_alerts=1,
                    test_alerts=counts.test_alerts,
                    reports=counts.reports,
                    dismissals=counts.dismissals,
                    train_reports=counts.train_reports,
                    test_reports=counts.test_reports,
                    transaction_rows=counts.transaction_rows,
                    future_rows=0,
                    older_than_365_rows=0,
                    alerts_without_transactions=0,
                )
            )

    def test_training_scale_ignores_later_rows_and_keeps_the_sentinel(self) -> None:
        self.assertEqual(list(inspect.signature(fit_training_scale).parameters), ["matrix"])
        train = np.array([[2.0, MISSING_GROUP_SENTINEL], [4.0, MISSING_GROUP_SENTINEL]], dtype=np.float64)
        scale = fit_training_scale(train)
        self.assertEqual(scale.mean, (3.0, MISSING_GROUP_SENTINEL))
        self.assertAlmostEqual(scale.scale[0], 1.0)
        self.assertEqual(scale.scale[1], 1.0)
        heldout = np.array([[100.0, 50.0]], dtype=np.float64)
        transformed = apply_training_scale(heldout, scale)
        self.assertEqual(scale.mean, (3.0, MISSING_GROUP_SENTINEL))
        self.assertAlmostEqual(transformed[0, 0], 97.0)
        self.assertAlmostEqual(transformed[0, 1], 53.0)
        raw = np.array([[1.0], [MISSING_GROUP_SENTINEL]], dtype=np.float64)
        self.assertEqual(float(raw[1, 0]), MISSING_GROUP_SENTINEL)


class RankingTests(unittest.TestCase):
    def test_orders_and_equal_capacity_metrics(self) -> None:
        self.assertEqual(
            chronological_order([10, 2], [date(2021, 1, 1), date(2021, 1, 1)]),
            (2, 10),
        )
        self.assertEqual(
            chronological_order([2, 1], [date(2021, 1, 2), date(2021, 1, 1)]),
            (1, 2),
        )
        self.assertEqual(model_order([5, 2], [0.5, 0.5]), (2, 5))
        self.assertEqual(model_order([2, 5], [0.1, 0.9]), (5, 2))

        alert_ids = tuple(range(1, 101))
        dates = tuple(date(2021, 1, 1) + timedelta(days=alert_id - 1) for alert_id in alert_ids)
        outcomes = {
            alert_id: "Report" if 50 <= alert_id <= 59 else "Dismiss" for alert_id in alert_ids
        }
        scores = tuple(0.9 if outcomes[alert_id] == "Report" else 0.1 for alert_id in alert_ids)
        comparisons = compare_capacities(alert_ids, dates, scores, outcomes, 10)
        five = comparisons[0]
        self.assertEqual(five.percent, 5)
        self.assertEqual(five.alerts_reviewed, 5)
        self.assertEqual(five.chronological.alerts_reviewed, five.supervised.alerts_reviewed)
        self.assertEqual(five.chronological.reports_identified, 0)
        self.assertEqual(five.supervised.reports_identified, 5)
        self.assertAlmostEqual(five.chronological.precision_at_k, 0.0)
        self.assertAlmostEqual(five.supervised.precision_at_k, 1.0)
        self.assertAlmostEqual(five.supervised.recall_at_k, 0.5)
        self.assertEqual(five.chronological.reports_remaining, 10)
        self.assertEqual(five.supervised.reports_remaining, 5)
        self.assertEqual(five.reports_identified_ml_minus_baseline, 5)
        self.assertEqual(five.reports_remaining_ml_minus_baseline, -5)
        hand = measure(tuple(range(1, 11)), {1: "Report", 2: "Dismiss", **{i: "Report" for i in range(3, 11)}}, 2, 9)
        self.assertEqual(hand.reports_identified, 1)
        self.assertAlmostEqual(hand.precision_at_k, 0.5)
        self.assertAlmostEqual(hand.recall_at_k, 1 / 9)
        self.assertEqual(hand.reports_remaining, 8)

    def test_two_fits_and_a_reloaded_artifact_match(self) -> None:
        matrix = np.zeros((30, 4), dtype=np.float64)
        labels = np.array([0] * 15 + [1] * 15)
        matrix[:15, 0] = -1.0
        matrix[15:, 0] = 1.0
        first_estimator, first = fit_report_model(matrix, labels)
        second_estimator, second = fit_report_model(matrix, labels)
        self.assertEqual(first.coefficients, second.coefficients)
        self.assertEqual(first.intercept, second.intercept)
        self.assertTrue(first.converged)
        left = score_report_model(first_estimator, matrix)
        right = score_report_model(second_estimator, matrix)
        np.testing.assert_allclose(left, right)
        manual = 1.0 / (1.0 + np.exp(-(first.intercept + matrix @ np.asarray(first.coefficients))))
        np.testing.assert_allclose(left, manual)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.joblib"
            from amlog.synthaml.preprocess import TrainingScale

            scale = TrainingScale(mean=(0.0, 0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0, 1.0), train_rows=30, train_columns=4)
            save_model_bundle(path, first_estimator, first, scale, ("a", "b", "c", "d"))
            bundle = load_model_bundle(path)

            def refuse_fit(*_args, **_kwargs):
                raise AssertionError("fit was called")

            bundle["model"].fit = refuse_fit
            np.testing.assert_allclose(score_saved_bundle(bundle, matrix), left)
        self.assertNotIn("y_train", bundle)
        self.assertTrue(all("outcome" not in name for name in bundle["feature_names"]))


class ExperimentTests(unittest.TestCase):
    def _write_population(self, root: Path, flip: bool = False) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        alerts = root / "alerts.csv"
        transactions = root / "transactions.csv"
        rows = []
        activity = []
        for offset in range(10):
            alert_id = offset + 1
            report = offset % 2 == 0
            outcome = "Report" if report != flip else "Dismiss"
            rows.append((alert_id, "2020-12-31" if offset == 0 else f"2020-01-{offset + 1:02d}", outcome))
            activity.append(
                (alert_id, f"2020-01-{offset + 1:02d} 00:00:01", "Credit", "Card", 2.0 if report else -1.0)
            )
        for offset in range(20):
            alert_id = offset + 11
            report = offset < 4
            outcome = "Report" if report != flip else "Dismiss"
            day = "2021-01-01" if offset == 0 else f"2021-02-{(offset % 27) + 1:02d}"
            rows.append((alert_id, day, outcome))
            activity.append((alert_id, f"{day} 00:00:01", "Debit", "Wire", 2.0 if report else -1.0))
        _alert_csv(alerts, rows)
        _transaction_csv(transactions, activity)
        return alerts, transactions

    def test_run_is_reproducible_and_keeps_outcomes_out_of_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alerts, transactions = self._write_population(root)
            flipped_alerts, _ = self._write_population(root / "flip", flip=True)
            first = run_experiment(alerts, transactions, root / "out1")
            second = run_experiment(alerts, transactions, root / "out2")
            changed = run_experiment(flipped_alerts, transactions, root / "out3")
            self.assertEqual(first.train_alerts, 10)
            self.assertEqual(first.test_alerts, 20)
            self.assertEqual(first.future_rows, 0)
            self.assertEqual(first.coefficient_count, 56)
            self.assertEqual(first.feature_matrix_sha256, second.feature_matrix_sha256)
            self.assertEqual(first.feature_matrix_sha256, changed.feature_matrix_sha256)
            document = json.loads(Path(first.evaluation_path).read_text(encoding="utf-8"))
            changed_document = json.loads(Path(changed.evaluation_path).read_text(encoding="utf-8"))
        self.assertEqual(
            [item.supervised.reports_identified for item in first.comparisons],
            [item.supervised.reports_identified for item in second.comparisons],
        )
        text = format_report(first)
        self.assertIn("SynthAML research benchmark", text)
        self.assertIn("not measured results for a financial institution", text)
        self.assertIn("Within a quarter, that order is not the bank's original review order", text)
        self.assertFalse(document["institution_result"])
        self.assertEqual(document["split"], "train dates in 2020; test dates in 2021; boundary 2021-01-01")
        self.assertNotIn("Outcome", document["feature_names"])
        self.assertEqual(len(document["model"]["coefficients"]), 56)
        self.assertNotEqual(document["model"]["coefficients"], changed_document["model"]["coefficients"])
        self.assertEqual(document["preprocessing"]["fit_on"], "2020 training alerts only")

    def test_a_future_transaction_stops_the_fit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alerts, transactions = self._write_population(root)
            extra = transactions.read_text(encoding="utf-8")
            transactions.write_text(
                extra + "2,2020-01-03 00:00:01,Credit,Card,50.0\n",
                encoding="utf-8",
            )
            with self.assertRaises(SynthamlLeakageError):
                run_experiment(alerts, transactions, root / "out")
            self.assertFalse((root / "out" / "synthaml_report_logistic-1.joblib").exists())

    def test_raw_files_and_operational_code_stay_separate(self) -> None:
        with self.assertRaises(SynthamlError):
            assert_outside_repository(REPO / "var" / "synthaml" / "synthetic_transactions.csv")
        with tempfile.TemporaryDirectory() as temporary:
            assert_outside_repository(Path(temporary) / "synthetic_transactions.csv")
        package = REPO / "src" / "amlog" / "synthaml"
        text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        for banned in (
            "psycopg",
            "AMLOG_DATABASE_URL",
            "INSERT INTO",
            "amlog_label",
            "train_supervised",
            "alert_behavior",
        ):
            self.assertNotIn(banned, text)
        supervised = (REPO / "src" / "amlog" / "prioritize" / "supervised.py").read_text(encoding="utf-8")
        self.assertIn("eligible_for_evaluation", supervised)
        self.assertIn("investigator_outcome", supervised)
        self.assertIn("Supervised training and false-positive reduction evaluation were not run", supervised)
        self.assertNotIn("synthaml", supervised.lower())
        evaluation = (REPO / "src" / "amlog" / "evaluate" / "outcomes.py").read_text(encoding="utf-8")
        self.assertIn("BLOCKED. A controlled investigation-outcome evaluation was not run.", evaluation)


class ResearchViewTests(unittest.TestCase):
    def _document(self) -> dict:
        return {
            "experiment_id": "phase11-synthaml-v1",
            "score_semantics": "Relative likelihood of Report on the research benchmark.",
            "limitations": ["Quarter dates were randomized."],
            "counts": {
                "alerts": 4,
                "train_alerts": 2,
                "test_alerts": 2,
                "reports": 2,
                "dismissals": 2,
                "train_reports": 1,
                "test_reports": 1,
            },
            "model": {
                "model_name": "synthaml_report_logistic",
                "model_version": "1",
                "algorithm_family": "logistic_regression",
                "sklearn_version": "1.7.2",
                "random_state": 20261026,
                "n_iter": 3,
                "converged": True,
            },
            "comparisons": [
                {
                    "percent": 5,
                    "alerts_reviewed": 1,
                    "reports_identified_ml_minus_baseline": 1,
                    "chronological": {
                        "reports_identified": 0,
                        "precision_at_k": 0.0,
                        "recall_at_k": 0.0,
                        "reports_remaining": 1,
                    },
                    "supervised": {
                        "reports_identified": 1,
                        "precision_at_k": 1.0,
                        "recall_at_k": 1.0,
                        "reports_remaining": 0,
                    },
                }
            ],
            "test_predictions": [
                {"alert_id": 20, "score": 0.2, "outcome": "Dismiss", "model_position": 2},
                {"alert_id": 9, "score": 0.8, "outcome": "Report", "model_position": 1},
            ],
        }

    def test_saved_artifact_renders_without_a_new_fit(self) -> None:
        from amlog.review.server import _render_synthaml
        from amlog.synthaml.exhibit import load_exhibit
        from amlog.synthaml.run import default_artifact_dir

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evaluation.json"
            path.write_text(json.dumps(self._document()), encoding="utf-8")
            exhibit = load_exhibit(path)
            page = _render_synthaml(path)
        self.assertIsNotNone(exhibit)
        assert exhibit is not None
        self.assertEqual(exhibit.sample[0].alert_id, 9)
        self.assertEqual(exhibit.sample[0].model_position, 1)
        self.assertEqual(exhibit.sample[0].outcome, "Report")
        self.assertAlmostEqual(exhibit.sample[0].score, 0.8)
        self.assertIn("SynthAML Research Benchmark", page)
        self.assertIn("AlertID", page)
        self.assertIn("0.800000", page)
        self.assertIn("no verified real-world productivity improvement", page)
        self.assertIn("synthesis-related leakage", page)
        self.assertIn("no account or customer identifier", page)
        self.assertNotIn("<form", page.lower())
        self.assertNotIn("amlog_label", page)
        source = (REPO / "src" / "amlog" / "synthaml" / "exhibit.py").read_text(encoding="utf-8")
        self.assertNotIn("fit_report_model", source)
        self.assertNotIn("LogisticRegression", source)
        missing = _render_synthaml(Path(temporary) / "absent.json")
        self.assertIn("not on this machine", missing)
        self.assertIn("does not fit a model", missing)
        saved = default_artifact_dir() / "synthaml-phase11-evaluation.json"
        if saved.is_file():
            live = load_exhibit(saved)
            assert live is not None
            self.assertEqual(live.alerts, 20000)
            self.assertEqual(live.train_alerts, 10839)
            self.assertEqual(live.test_alerts, 9161)
            self.assertEqual(live.sample[0].model_position, 1)
            self.assertEqual(len(live.comparisons), 3)

    def test_research_route_does_not_open_the_database(self) -> None:
        import threading
        import urllib.error
        import urllib.request

        from amlog.review.server import ReviewServer

        server = ReviewServer(("127.0.0.1", 0), "postgresql://invalid/none")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            page = urllib.request.urlopen(base + "/synthaml", timeout=5).read().decode("utf-8")
            self.assertIn("SynthAML Research Benchmark", page)
            self.assertNotIn("<form", page.lower())
            request = urllib.request.Request(base + "/synthaml", data=b"outcome=Report", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

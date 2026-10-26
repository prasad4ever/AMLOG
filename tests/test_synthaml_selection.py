"""Model-selection checks that do not need the published transaction file."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

from amlog.synthaml.errors import SynthamlLeakageError
from amlog.synthaml.preprocess import fit_training_scale
from amlog.synthaml.selection import (
    CANDIDATES,
    SELECTION_FOLDS,
    FoldObservation,
    average_precision,
    choose,
    fit_candidate,
    forward_fold_masks,
    reports_at_percent,
    score_fold,
    score_saved_selection,
)
from amlog.synthaml.select_run import evaluate_holdout_after_lock, select_on_2020

REPO = Path(__file__).resolve().parents[1]


def _observation(candidate: str, fold: str, reports_at_10: int, precision: float) -> FoldObservation:
    return FoldObservation(
        candidate=candidate,
        fold=fold,
        alerts=100,
        reports=20,
        reports_at_5=1,
        reports_at_10=reports_at_10,
        reports_at_20=4,
        precision_at_5=0.1,
        precision_at_10=precision,
        precision_at_20=0.2,
        recall_at_5=0.05,
        recall_at_10=0.1,
        recall_at_20=0.2,
        average_precision=0.3,
        train_precision_at_10=precision,
    )


def _table(reports: dict[str, int], precision: dict[str, float]) -> list[FoldObservation]:
    rows = []
    for candidate in CANDIDATES:
        for index, fold in enumerate(SELECTION_FOLDS):
            rows.append(_observation(candidate, fold, reports[candidate], precision[candidate] + index * 0.0))
    return rows


class SelectionRuleTests(unittest.TestCase):
    def test_primary_metric_prefers_the_higher_report_count(self) -> None:
        reports = {name: 40 for name in CANDIDATES}
        reports["random_forest"] = 80
        precision = {name: 0.2 for name in CANDIDATES}
        decision = choose(_table(reports, precision))
        self.assertEqual(decision.candidate, "random_forest")
        self.assertEqual(decision.total_reports_at_10, 240)

    def test_a_near_tie_keeps_the_simpler_candidate(self) -> None:
        reports = {name: 40 for name in CANDIDATES}
        reports["random_forest"] = 42
        reports["logistic_regression"] = 40
        precision = {name: 0.25 for name in CANDIDATES}
        decision = choose(_table(reports, precision))
        self.assertEqual(decision.candidate, "logistic_regression")

    def test_the_same_rows_in_another_order_name_the_same_candidate(self) -> None:
        reports = {name: 30 for name in CANDIDATES}
        reports["extra_trees"] = 70
        precision = {name: 0.2 for name in CANDIDATES}
        rows = _table(reports, precision)
        forward = choose(rows)
        backward = choose(list(reversed(rows)))
        self.assertEqual(forward.candidate, backward.candidate)
        self.assertEqual(forward.total_reports_at_10, backward.total_reports_at_10)

    def test_a_2021_fold_cannot_enter_the_choice(self) -> None:
        rows = _table({name: 10 for name in CANDIDATES}, {name: 0.2 for name in CANDIDATES})
        leaked = rows[:-1] + [_observation(rows[-1].candidate, "2021Q1", 10, 0.2)]
        with self.assertRaises(SynthamlLeakageError):
            choose(leaked)

    def test_selection_code_does_not_read_the_holdout(self) -> None:
        source = inspect.getsource(select_on_2020)
        self.assertNotIn("2021", source)
        self.assertNotIn("evaluate_holdout_after_lock", source)
        runner = inspect.getsource(
            __import__("amlog.synthaml.select_run", fromlist=["run_selection"]).run_selection
        )
        self.assertLess(runner.index("select_on_2020("), runner.index("evaluate_holdout_after_lock("))


class MetricTests(unittest.TestCase):
    def test_reports_at_ten_percent_match_a_hand_count(self) -> None:
        scores = np.array([0.9, 0.1, 0.8, 0.2, 0.7, 0.3, 0.6, 0.4, 0.5, 0.05])
        outcomes = np.array([1, 0, 1, 0, 0, 0, 0, 0, 1, 0])
        identifiers = np.arange(10)
        captured, reviewed, precision, recall = reports_at_percent(scores, outcomes, identifiers, 10)
        self.assertEqual(captured, 1)
        self.assertEqual(reviewed, 1)
        self.assertEqual(precision, 1.0)
        self.assertAlmostEqual(recall, 1 / 3)
        captured_20, reviewed_20, _precision, _recall = reports_at_percent(
            scores, outcomes, identifiers, 20
        )
        self.assertEqual((captured_20, reviewed_20), (2, 2))

    def test_average_precision_of_a_perfect_order_is_one(self) -> None:
        self.assertEqual(average_precision(np.array([1, 1, 0, 0]), np.array([0.9, 0.8, 0.2, 0.1])), 1.0)

    def test_accuracy_is_not_a_selection_field(self) -> None:
        source = (REPO / "src" / "amlog" / "synthaml" / "selection.py").read_text(encoding="utf-8")
        self.assertNotIn("accuracy_score", source)


class TemporalLeakageTests(unittest.TestCase):
    def test_forward_folds_keep_validation_after_training(self) -> None:
        dates = []
        for month in (1, 4, 7, 10):
            dates.extend(date(2020, month, day) for day in range(1, 11))
        folds = forward_fold_masks(dates)
        self.assertEqual(tuple(name for name, _train, _valid in folds), SELECTION_FOLDS)
        for _name, train, valid in folds:
            self.assertLess(max(dates[index] for index in np.flatnonzero(train)), min(dates[index] for index in np.flatnonzero(valid)))
            self.assertFalse(np.logical_and(train, valid).any())

    def test_a_2021_date_is_rejected_before_a_fold_is_built(self) -> None:
        dates = [date(2020, 1, 15), date(2021, 1, 1)]
        with self.assertRaises(SynthamlLeakageError):
            forward_fold_masks(dates)

    def test_preprocessing_mean_ignores_the_validation_quarter(self) -> None:
        dates = []
        identifiers = []
        outcomes = []
        rows = []
        for quarter, month in enumerate((1, 4, 7, 10)):
            for offset in range(20):
                dates.append(date(2020, month, offset + 1))
                identifiers.append(quarter * 10 + offset)
                outcomes.append(1 if offset < 2 else 0)
                rows.append([0.0 if quarter == 0 else 100.0, float(offset)])
        folds = forward_fold_masks(dates)
        _name, train, valid = folds[0]
        _rows, _members, scale = score_fold(
            np.asarray(rows, dtype=np.float64),
            np.asarray(outcomes),
            np.asarray(identifiers),
            dates,
            train,
            valid,
            "2020Q2",
        )
        self.assertEqual(scale.mean[0], 0.0)
        self.assertEqual(int(train.sum()), 20)

    def test_select_on_2020_rejects_a_later_year(self) -> None:
        dates = np.asarray([date(2020, 1, 1), date(2021, 4, 1)], dtype=object)
        with self.assertRaises(SynthamlLeakageError):
            select_on_2020(dates, np.array([1, 2]), np.array([1, 0]), np.ones((2, 2)))


class ArtifactReloadTests(unittest.TestCase):
    def test_saved_selection_scores_without_fitting_again(self) -> None:
        from amlog.synthaml.select_run import _save_selected_model

        matrix = np.array(
            [
                [0.0, 0.0],
                [0.1, 0.2],
                [3.0, 3.0],
                [3.2, 2.8],
                [0.2, 0.1],
                [2.7, 3.1],
            ],
            dtype=np.float64,
        )
        outcomes = np.array([0, 0, 1, 1, 0, 1])
        model = fit_candidate("logistic_regression", matrix, outcomes)
        scale = fit_training_scale(matrix)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "selected.joblib"
            _save_selected_model(path, "logistic_regression", scale, {"logistic_regression": model})
            import joblib

            bundle = joblib.load(path)
        expected = score_saved_selection(bundle, matrix)

        def refuse_fit(*_args, **_kwargs):
            raise AssertionError("The reloaded model was fit again.")

        bundle["members"]["logistic_regression"].fit = refuse_fit
        scored = score_saved_selection(bundle, matrix)
        np.testing.assert_allclose(scored, expected)
        self.assertFalse(bundle["approved_for_operational_use"])

    def test_holdout_evaluation_does_not_change_the_locked_name(self) -> None:
        dates = []
        identifiers = []
        outcomes = []
        rows = []
        for year, count in ((2020, 80), (2021, 20)):
            for offset in range(count):
                month = (offset % 4) * 3 + 1
                dates.append(date(year, month, (offset % 27) + 1))
                identifiers.append(year * 100 + offset)
                outcomes.append(1 if offset % 5 == 0 else 0)
                rows.append([float(offset % 3), float(outcomes[-1])])
        train_dates = [day for day in dates if day.year == 2020]
        decision, _observations, _date_order = select_on_2020(
            np.asarray(train_dates, dtype=object),
            np.asarray([identifier for identifier, day in zip(identifiers, dates, strict=True) if day.year == 2020]),
            np.asarray([code for code, day in zip(outcomes, dates, strict=True) if day.year == 2020]),
            np.asarray([row for row, day in zip(rows, dates, strict=True) if day.year == 2020], dtype=np.float64),
        )
        locked = decision.candidate
        years = np.asarray([day.year for day in dates])
        holdout = evaluate_holdout_after_lock(
            decision,
            np.asarray(rows, dtype=np.float64),
            np.asarray(outcomes),
            np.asarray(identifiers),
            np.asarray(dates, dtype=object),
            years == 2020,
            years == 2021,
            {identifier: "Report" if code else "Dismiss" for identifier, code in zip(identifiers, outcomes, strict=True)},
        )
        self.assertEqual(decision.candidate, locked)
        selected_rows = [row for row in holdout["metrics"] if row.get("selected")]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["candidate"], locked)


class ResearchPageTests(unittest.TestCase):
    def test_page_distinguishes_the_selected_model_from_the_baseline(self) -> None:
        from amlog.review.server import _render_synthaml

        evaluation = {
            "experiment_id": "phase11-synthaml-v1",
            "score_semantics": "Relative likelihood of Report on the research benchmark.",
            "limitations": ["Quarter dates were randomized."],
            "counts": {
                "alerts": 20,
                "train_alerts": 10,
                "test_alerts": 10,
                "reports": 4,
                "dismissals": 16,
                "train_reports": 2,
                "test_reports": 2,
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
                    "percent": percent,
                    "alerts_reviewed": 1,
                    "reports_identified_ml_minus_baseline": 1,
                    "chronological": {
                        "reports_identified": 0,
                        "precision_at_k": 0.0,
                        "recall_at_k": 0.0,
                        "reports_remaining": 2,
                    },
                    "supervised": {
                        "reports_identified": 1,
                        "precision_at_k": 1.0,
                        "recall_at_k": 0.5,
                        "reports_remaining": 1,
                    },
                }
                for percent in (5, 10, 20)
            ],
            "test_predictions": [
                {
                    "alert_id": 9,
                    "score": 0.8,
                    "outcome": "Report",
                    "model_position": 1,
                }
            ],
        }
        selection = {
            "experiment_id": "phase11c-synthaml-v1",
            "holdout_disclosure": "The 2021 outcomes were already inspected for the logistic baseline.",
            "selected_candidate": "extra_trees",
            "primary_metric": "reports_captured_at_10_percent",
            "selection_reason": "extra_trees captured 90 Report outcomes at 10 percent.",
            "validation_totals": [
                {
                    "candidate": "extra_trees",
                    "reports_at_10": 90,
                    "precision_at_10_std": 0.01,
                    "average_precision_mean": 0.4,
                }
            ],
            "date_order_validation": [{"reports_at_10": 40}],
            "holdout_2021": [
                {
                    "candidate": "date_order",
                    "reports_at_5": 1,
                    "reports_at_10": 2,
                    "reports_at_20": 3,
                    "precision_at_10": 0.2,
                    "recall_at_10": 0.1,
                    "average_precision": 0.2,
                },
                {
                    "candidate": "extra_trees",
                    "reports_at_5": 2,
                    "reports_at_10": 4,
                    "reports_at_20": 6,
                    "precision_at_10": 0.4,
                    "recall_at_10": 0.2,
                    "average_precision": 0.5,
                    "selected": True,
                },
            ],
            "selected_sample": [
                {"rank": 1, "alert_id": 15, "score": 0.66, "outcome": "Dismiss"}
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluation_path = root / "synthaml-phase11-evaluation.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            (root / "synthaml-phase11c-selection.json").write_text(json.dumps(selection), encoding="utf-8")
            page = _render_synthaml(evaluation_path)
        self.assertIn("Selected experimental model", page)
        self.assertIn("extra_trees", page)
        self.assertIn("Logistic regression baseline", page)
        self.assertIn("synthaml_report_logistic", page)
        self.assertIn("already inspected", page)
        self.assertNotIn("<form", page.lower())
        self.assertNotIn("amlog_label", page)


if __name__ == "__main__":
    unittest.main()

"""Tests for the fictional workload generator.

These tests do not connect to PostgreSQL.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.ingest.contract import parse_transaction_event  # noqa: E402
from amlog.workload.generate import (  # noqa: E402
    SCENARIO_CODES,
    WorkloadSpec,
    iter_events,
)
from amlog.workload.load import (  # noqa: E402
    WorkloadTargetError,
    assert_workload_database_is_disposable,
)


def spec(**overrides) -> WorkloadSpec:
    values = {
        "seed": 11,
        "population_id": "study-a",
        "event_count": 160,
    }
    values.update(overrides)
    return WorkloadSpec(**values)


class WorkloadGeneratorTests(unittest.TestCase):
    def test_the_stream_is_produced_one_event_at_a_time(self) -> None:
        stream = iter_events(spec(event_count=40))
        self.assertTrue(inspect.isgenerator(stream))
        first = next(stream)
        self.assertIsNotNone(first.payload["source_event_id"])

    def test_the_same_seed_and_population_repeat(self) -> None:
        first = [_identity(event) for event in iter_events(spec())]
        second = [_identity(event) for event in iter_events(spec())]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 160)

    def test_a_different_seed_or_population_is_independent(self) -> None:
        base = [_identity(event) for event in iter_events(spec(event_count=40))]
        other_seed = [_identity(event) for event in iter_events(spec(event_count=40, seed=12))]
        other_population = [
            _identity(event) for event in iter_events(spec(event_count=40, population_id="study-b"))
        ]
        self.assertNotEqual(base, other_seed)
        self.assertNotEqual(base, other_population)

    def test_events_cover_profiles_channels_currencies_and_scenarios(self) -> None:
        events = list(iter_events(spec()))
        profiles = {event.profile_code for event in events}
        scenarios = {event.scenario_code for event in events}
        channels = {event.payload["payment_channel"] for event in events}
        currencies = {event.payload["currency_code"] for event in events}
        self.assertEqual(scenarios, set(SCENARIO_CODES))
        self.assertGreaterEqual(len(profiles), 7)
        self.assertGreaterEqual(channels, {"card", "ach", "wire", "wallet"})
        self.assertGreaterEqual(currencies, {"USD", "EUR", "GBP", "CAD"})

    def test_timestamps_follow_account_opening_and_stay_ordered(self) -> None:
        by_account: dict[str, list] = defaultdict(list)
        for event in iter_events(spec()):
            self.assertGreaterEqual(event.event_time, event.account_opened_at)
            self.assertGreaterEqual(event.event_time, datetime(2025, 1, 1, tzinfo=timezone.utc))
            by_account[event.payload["account_reference"]].append(event.event_time)
        for times in by_account.values():
            self.assertEqual(times, sorted(times))
            self.assertEqual(len(times), len(set(times)))

    def test_a_late_payment_is_not_placed_one_second_after_the_previous_one(self) -> None:
        by_account: dict[str, list] = defaultdict(list)
        history_end = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
        for event in iter_events(spec(event_count=2000, population_id="gap-check")):
            by_account[event.payload["account_reference"]].append(event.event_time)
        for times in by_account.values():
            ordered = sorted(times)
            for earlier, later in zip(ordered, ordered[1:]):
                if later - earlier == timedelta(seconds=1):
                    self.assertGreater(earlier, history_end - timedelta(hours=2))

    def test_signature_behaviors_have_distinct_shapes(self) -> None:
        events = list(iter_events(spec()))
        salary = [event for event in events if event.scenario_code == "salary"]
        seasonal = [event for event in events if event.scenario_code == "seasonal_spend"]
        unusual = [event for event in events if event.scenario_code == "unusual_amount"]
        regular = [event for event in events if event.scenario_code == "regular_spend"]
        rapid = [event for event in events if event.profile_code == "marketplace"][:2]
        cross_border = [event for event in events if event.scenario_code == "cross_border"]
        self.assertTrue(salary)
        self.assertTrue(all(event.event_time.day == 15 for event in salary))
        self.assertTrue(all(event.event_time.month == 12 for event in seasonal))
        self.assertTrue(all(Decimal(event.payload["amount"]) >= Decimal("2500.00") for event in unusual))
        self.assertTrue(all(Decimal(event.payload["amount"]) <= Decimal("120.00") for event in regular))
        self.assertEqual(rapid[1].event_time - rapid[0].event_time, timedelta(hours=20))
        self.assertEqual(rapid[0].payload["direction"], "credit")
        self.assertEqual(rapid[1].payload["direction"], "debit")
        self.assertNotEqual(
            rapid[0].payload["account_reference"],
            rapid[1].payload["account_reference"],
        )
        self.assertTrue(cross_border)
        for event in cross_border:
            self.assertNotEqual(event.payload["currency_code"], event.payload["base_currency_code"])
            self.assertIn("fx_rate", event.payload)

    def test_payment_events_do_not_carry_labels_or_scenario_notes(self) -> None:
        forbidden = {
            "customer_id",
            "account_id",
            "label",
            "label_value",
            "fraud",
            "is_fraud",
            "risk_score",
            "score",
            "scenario_code",
            "profile_code",
        }
        for event in iter_events(spec(event_count=40)):
            self.assertTrue(forbidden.isdisjoint(event.payload))
            parsed = parse_transaction_event(event.payload)
            self.assertIsInstance(parsed.amount, Decimal)
            self.assertEqual(parsed.schema_version, "2")
            self.assertEqual(parsed.account_reference, event.payload["account_reference"])
            self.assertEqual(parsed.source_account_id, event.payload["source_account_id"])

    def test_there_is_no_fixed_event_ceiling(self) -> None:
        large = spec(event_count=100_001, population_id="scale-check")
        self.assertEqual(large.event_count, 100_001)
        stream = iter_events(large)
        self.assertIsNotNone(next(stream))
        stream.close()

    def test_the_development_database_is_refused(self) -> None:
        with self.assertRaises(WorkloadTargetError):
            assert_workload_database_is_disposable("amlog", "amlog")
        with self.assertRaises(WorkloadTargetError):
            assert_workload_database_is_disposable("postgres", "other")
        assert_workload_database_is_disposable("amlog_workload", "amlog")


def _identity(event) -> tuple:
    return (
        event.payload["source_system"],
        event.payload["source_event_id"],
        event.payload["amount"],
        event.payload["currency_code"],
        event.scenario_code,
        event.event_time.isoformat(),
    )


if __name__ == "__main__":
    unittest.main()

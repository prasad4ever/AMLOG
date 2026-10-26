"""R001 window logic without a database."""

from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.monitor.engine import TRANSACTION_QUERY  # noqa: E402
from amlog.monitor.velocity import (  # noqa: E402
    R001_PARAMETERS,
    ObservedTransaction,
    VelocityParameters,
    find_velocity_bursts,
    parse_velocity_parameters,
)


ORIGIN = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000001")
CUSTOMER = uuid.UUID("00000000-0000-4000-8000-000000000002")


def parameters(**overrides: object) -> VelocityParameters:
    document = dict(R001_PARAMETERS)
    document.update(overrides)
    return parse_velocity_parameters(document)


def debit(minutes: float, *, direction: str = "debit", account: uuid.UUID = ACCOUNT) -> ObservedTransaction:
    return ObservedTransaction(
        transaction_id=uuid.uuid4(),
        account_id=account,
        customer_id=CUSTOMER,
        direction=direction,
        event_time=ORIGIN + timedelta(minutes=minutes),
    )


class VelocityRuleTests(unittest.TestCase):
    def test_registered_version_uses_a_sixty_minute_window_and_six_debits(self) -> None:
        parsed = parse_velocity_parameters(R001_PARAMETERS)
        self.assertEqual(parsed.direction, "debit")
        self.assertEqual(parsed.window, timedelta(hours=1))
        self.assertEqual(parsed.minimum_count, 6)

    def test_five_debits_do_not_open_a_burst(self) -> None:
        rows = [debit(index * 10) for index in range(5)]
        self.assertEqual(find_velocity_bursts(rows, parameters()), ())

    def test_sixth_debit_exactly_sixty_minutes_later_opens_one_burst(self) -> None:
        rows = [debit(index * 12) for index in range(5)]
        rows.append(debit(60))
        bursts = find_velocity_bursts(rows, parameters())
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0].members, tuple(rows))
        self.assertEqual(bursts[0].triggering, rows[-1])
        self.assertEqual(bursts[0].window_ended_at - bursts[0].window_started_at, timedelta(hours=1))

    def test_sixth_debit_one_second_outside_the_window_does_not_open_a_burst(self) -> None:
        rows = [debit(index * 12) for index in range(5)]
        rows.append(debit(60 + (1 / 60)))
        self.assertEqual(find_velocity_bursts(rows, parameters()), ())

    def test_credits_do_not_count_and_do_not_break_a_debit_window(self) -> None:
        rows = [debit(0), debit(1), debit(2), debit(3, direction="credit"), debit(4), debit(5), debit(6)]
        bursts = find_velocity_bursts(rows, parameters())
        self.assertEqual(len(bursts), 1)
        self.assertTrue(all(member.direction == "debit" for member in bursts[0].members))
        self.assertEqual(len(bursts[0].members), 6)
        self.assertNotIn(rows[3].transaction_id, [member.transaction_id for member in bursts[0].members])

    def test_a_later_cluster_is_not_visible_to_an_earlier_event(self) -> None:
        early = [debit(index) for index in range(5)]
        late = [debit(180 + index) for index in range(6)]
        bursts = find_velocity_bursts(early + late, parameters())
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0].triggering, late[-1])
        self.assertTrue(all(member.event_time >= late[0].event_time for member in bursts[0].members))

    def test_transactions_must_be_in_event_time_order(self) -> None:
        rows = [debit(2), debit(1)]
        with self.assertRaises(ValueError):
            find_velocity_bursts(rows, parameters())

    def test_one_continued_burst_does_not_open_a_second_alert(self) -> None:
        rows = [debit(index) for index in range(7)]
        bursts = find_velocity_bursts(rows, parameters())
        self.assertEqual(len(bursts), 1)
        self.assertEqual(len(bursts[0].members), 6)
        self.assertNotIn(rows[-1], bursts[0].members)

    def test_a_quiet_gap_allows_a_second_burst(self) -> None:
        first = [debit(index) for index in range(6)]
        second = [debit(180 + index) for index in range(6)]
        bursts = find_velocity_bursts(first + second, parameters())
        self.assertEqual(len(bursts), 2)
        self.assertEqual(bursts[0].triggering, first[-1])
        self.assertEqual(bursts[1].triggering, second[-1])

    def test_the_same_events_produce_the_same_bursts(self) -> None:
        rows = [debit(index * 5) for index in range(6)]
        self.assertEqual(
            find_velocity_bursts(rows, parameters()),
            find_velocity_bursts(rows, parameters()),
        )

    def test_threshold_and_window_come_from_the_parameters(self) -> None:
        rows = [debit(0), debit(0.5), debit(1.5)]
        self.assertEqual(len(find_velocity_bursts(rows, parameters(minimum_count=3, window_seconds=120))), 1)
        self.assertEqual(find_velocity_bursts(rows, parameters(minimum_count=6, window_seconds=120)), ())
        self.assertEqual(find_velocity_bursts(rows, parameters(minimum_count=3, window_seconds=60)), ())

    def test_a_parameter_document_with_extra_or_invalid_values_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_velocity_parameters({"direction": "debit", "window_seconds": 60})
        with self.assertRaises(ValueError):
            parse_velocity_parameters({**R001_PARAMETERS, "note": "ignored"})
        with self.assertRaises(ValueError):
            parse_velocity_parameters({**R001_PARAMETERS, "minimum_count": True})
        with self.assertRaises(ValueError):
            parse_velocity_parameters({**R001_PARAMETERS, "window_seconds": 0})

    def test_the_transaction_query_does_not_read_scenarios_or_labels(self) -> None:
        lowered = TRANSACTION_QUERY.lower()
        self.assertNotIn("amlog_scenario", lowered)
        self.assertNotIn("amlog_label", lowered)
        self.assertNotIn("scenario_code", lowered)
        self.assertIn("event_timestamp", lowered)
        self.assertNotIn("ingested_at", lowered)


if __name__ == "__main__":
    unittest.main()

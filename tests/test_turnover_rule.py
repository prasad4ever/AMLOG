"""R002 allocation logic without a database."""

from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.monitor.engine import TURNOVER_TRANSACTION_QUERY  # noqa: E402
from amlog.monitor.turnover import (  # noqa: E402
    R002_PARAMETERS,
    TurnoverTransaction,
    find_turnover_episodes,
    parse_turnover_parameters,
)


ORIGIN = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000011")
CUSTOMER = uuid.UUID("00000000-0000-4000-8000-000000000012")


def parameters(**overrides: object):
    document = dict(R002_PARAMETERS)
    document.update(overrides)
    return parse_turnover_parameters(document)


def payment(
    hours: float,
    *,
    direction: str,
    amount: str,
    currency: str = "USD",
    account: uuid.UUID = ACCOUNT,
    seconds: float = 0,
) -> TurnoverTransaction:
    return TurnoverTransaction(
        transaction_id=uuid.uuid4(),
        account_id=account,
        customer_id=CUSTOMER,
        direction=direction,
        event_time=ORIGIN + timedelta(hours=hours, seconds=seconds),
        amount=Decimal(amount),
        currency_code=currency,
    )


class TurnoverRuleTests(unittest.TestCase):
    def test_registered_version_uses_five_hundred_eighty_percent_and_twenty_four_hours(self) -> None:
        parsed = parse_turnover_parameters(R002_PARAMETERS)
        self.assertEqual(parsed.minimum_incoming_amount, Decimal("500"))
        self.assertEqual(parsed.outgoing_ratio, Decimal("0.80"))
        self.assertEqual(parsed.window, timedelta(hours=24))
        self.assertEqual(parsed.amount_basis, "transaction_currency")

    def test_exact_ratio_alerts_and_the_trigger_is_the_outgoing_payment(self) -> None:
        incoming = payment(0, direction="credit", amount="500")
        outgoing = payment(2, direction="debit", amount="400")
        episodes = find_turnover_episodes([incoming, outgoing], parameters())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].triggering, outgoing)
        self.assertEqual(episodes[0].incoming, incoming)
        self.assertGreater(episodes[0].triggering.event_time, episodes[0].incoming.event_time)
        self.assertEqual(episodes[0].allocated_outgoing, Decimal("400"))

    def test_just_below_the_ratio_and_just_below_the_incoming_minimum_do_not_alert(self) -> None:
        below_ratio = [
            payment(0, direction="credit", amount="500"),
            payment(2, direction="debit", amount="399.99"),
        ]
        below_minimum = [
            payment(0, direction="credit", amount="499.99"),
            payment(2, direction="debit", amount="499.99"),
        ]
        self.assertEqual(find_turnover_episodes(below_ratio, parameters()), ())
        self.assertEqual(find_turnover_episodes(below_minimum, parameters()), ())

    def test_exact_twenty_four_hours_counts_and_one_second_later_does_not(self) -> None:
        incoming = payment(0, direction="credit", amount="500")
        on_time = payment(24, direction="debit", amount="400")
        late_incoming = payment(0, direction="credit", amount="500")
        late = payment(24, direction="debit", amount="400", seconds=1)
        self.assertEqual(len(find_turnover_episodes([incoming, on_time], parameters())), 1)
        self.assertEqual(find_turnover_episodes([late_incoming, late], parameters()), ())

    def test_an_earlier_outgoing_payment_is_not_matched_backwards(self) -> None:
        rows = [
            payment(0, direction="debit", amount="500"),
            payment(1, direction="credit", amount="500"),
        ]
        self.assertEqual(find_turnover_episodes(rows, parameters()), ())

    def test_a_different_currency_does_not_count_even_when_the_amounts_match(self) -> None:
        rows = [
            payment(0, direction="credit", amount="1000", currency="USD"),
            payment(1, direction="debit", amount="1000", currency="EUR"),
        ]
        self.assertEqual(find_turnover_episodes(rows, parameters()), ())

    def test_several_outgoing_payments_reach_the_ratio_on_the_later_one(self) -> None:
        incoming = payment(0, direction="credit", amount="500")
        first = payment(1, direction="debit", amount="200")
        second = payment(2, direction="debit", amount="200")
        episodes = find_turnover_episodes([incoming, first, second], parameters())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].triggering, second)
        self.assertEqual(
            [part.transaction for part in episodes[0].outgoing],
            [first, second],
        )
        self.assertEqual(
            [part.allocated_amount for part in episodes[0].outgoing],
            [Decimal("200"), Decimal("200")],
        )

    def test_one_outgoing_amount_is_not_allocated_to_two_incoming_payments(self) -> None:
        first = payment(0, direction="credit", amount="500")
        second = payment(1, direction="credit", amount="500")
        debit = payment(2, direction="debit", amount="500")
        episodes = find_turnover_episodes([first, second, debit], parameters())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].incoming, first)
        self.assertEqual(episodes[0].allocated_outgoing, Decimal("500"))

    def test_the_unallocated_remainder_can_qualify_the_next_incoming_payment(self) -> None:
        first = payment(0, direction="credit", amount="500")
        second = payment(1, direction="credit", amount="500")
        debit = payment(2, direction="debit", amount="900")
        episodes = find_turnover_episodes([first, second, debit], parameters())
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].allocated_outgoing, Decimal("500"))
        self.assertEqual(episodes[1].allocated_outgoing, Decimal("400"))
        self.assertEqual(episodes[0].triggering, debit)
        self.assertEqual(episodes[1].triggering, debit)
        self.assertEqual(
            episodes[0].allocated_outgoing + episodes[1].allocated_outgoing,
            debit.amount,
        )

    def test_later_outgoing_amount_in_the_same_episode_does_not_open_a_second_alert(self) -> None:
        rows = [
            payment(0, direction="credit", amount="500"),
            payment(1, direction="debit", amount="400"),
            payment(2, direction="debit", amount="100"),
        ]
        episodes = find_turnover_episodes(rows, parameters())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(episodes[0].outgoing), 1)
        self.assertEqual(episodes[0].allocated_outgoing, Decimal("400"))

    def test_small_incoming_payments_are_not_added_together(self) -> None:
        rows = [
            payment(0, direction="credit", amount="300"),
            payment(1, direction="credit", amount="300"),
            payment(2, direction="debit", amount="480"),
        ]
        self.assertEqual(find_turnover_episodes(rows, parameters()), ())

    def test_the_same_events_produce_the_same_episodes(self) -> None:
        rows = [
            payment(0, direction="credit", amount="800"),
            payment(3, direction="debit", amount="700"),
        ]
        self.assertEqual(
            find_turnover_episodes(rows, parameters()),
            find_turnover_episodes(rows, parameters()),
        )

    def test_transactions_must_be_in_event_time_order(self) -> None:
        rows = [
            payment(2, direction="debit", amount="400"),
            payment(0, direction="credit", amount="500"),
        ]
        with self.assertRaises(ValueError):
            find_turnover_episodes(rows, parameters())

    def test_threshold_and_window_come_from_the_parameters(self) -> None:
        rows = [
            payment(0, direction="credit", amount="100"),
            payment(0.5, direction="debit", amount="50"),
        ]
        short = parameters(minimum_incoming_amount="100", outgoing_ratio="0.50", window_seconds=3600)
        narrow = parameters(minimum_incoming_amount="100", outgoing_ratio="0.50", window_seconds=60)
        version_one = parameters()
        self.assertEqual(len(find_turnover_episodes(rows, short)), 1)
        self.assertEqual(find_turnover_episodes(rows, narrow), ())
        self.assertEqual(find_turnover_episodes(rows, version_one), ())

    def test_currency_conversion_and_invalid_parameters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_turnover_parameters({**R002_PARAMETERS, "amount_basis": "base_currency"})
        with self.assertRaises(ValueError):
            parse_turnover_parameters({**R002_PARAMETERS, "minimum_incoming_amount": 500.0})
        with self.assertRaises(ValueError):
            parse_turnover_parameters({**R002_PARAMETERS, "outgoing_ratio": True})
        with self.assertRaises(ValueError):
            parse_turnover_parameters({"minimum_incoming_amount": "500"})

    def test_the_transaction_query_does_not_read_scenarios_labels_or_exchange_rates(self) -> None:
        lowered = TURNOVER_TRANSACTION_QUERY.lower()
        self.assertNotIn("amlog_scenario", lowered)
        self.assertNotIn("amlog_label", lowered)
        self.assertNotIn("scenario_code", lowered)
        self.assertNotIn("base_amount", lowered)
        self.assertNotIn("fx_rate", lowered)
        self.assertIn("currency_code", lowered)
        self.assertIn("event_timestamp", lowered)


if __name__ == "__main__":
    unittest.main()

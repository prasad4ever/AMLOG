"""Same-account workload shapes judged by the unchanged R002 function.

The scenario code is visible to this test. It is not passed into the rule.
"""

from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog.monitor.turnover import (  # noqa: E402
    R002_PARAMETERS,
    TurnoverTransaction,
    find_turnover_episodes,
    parse_turnover_parameters,
)
from amlog.monitor.velocity import (  # noqa: E402
    R001_PARAMETERS,
    ObservedTransaction,
    find_velocity_bursts,
    parse_velocity_parameters,
)
from amlog.workload.generate import WorkloadSpec, iter_events  # noqa: E402


_QUALIFIES = {"same_account_exact", "same_account_above"}
_DOES_NOT_QUALIFY = {
    "same_account_below",
    "same_account_late",
    "supplier_follow_on",
    "recurring_after_receipt",
    "cross_currency_outgoing",
}
_PAIR_CODES = _QUALIFIES | _DOES_NOT_QUALIFY


class SameAccountWorkloadTests(unittest.TestCase):
    def test_same_account_shapes_are_separate_from_cross_account_movement(self) -> None:
        events = list(iter_events(WorkloadSpec(seed=11, population_id="shape-check", event_count=160)))
        pairs = _pairs(events)
        codes = {code for code, _pair in pairs}
        self.assertTrue(_PAIR_CODES <= codes)
        rapid = [
            event
            for event in events
            if event.profile_code == "marketplace" and event.scenario_code == "rapid_movement"
        ][:2]
        self.assertNotEqual(rapid[0].payload["account_reference"], rapid[1].payload["account_reference"])

        currencies = set()
        customer_types = set()
        for code, (receipt, outgoing) in pairs:
            self.assertEqual(receipt.payload["direction"], "credit")
            self.assertEqual(outgoing.payload["direction"], "debit")
            self.assertGreaterEqual(Decimal(receipt.payload["amount"]), Decimal("500"))
            self.assertNotEqual(receipt.payload["counterparty_reference"], outgoing.payload["counterparty_reference"])
            if code == "rapid_movement":
                self.assertNotEqual(
                    receipt.payload["account_reference"],
                    outgoing.payload["account_reference"],
                )
                continue
            if code == "cross_currency_outgoing":
                self.assertEqual(receipt.payload["account_reference"], outgoing.payload["account_reference"])
                self.assertNotEqual(receipt.payload["currency_code"], outgoing.payload["currency_code"])
                self.assertIn("fx_rate", outgoing.payload)
                continue
            self.assertEqual(receipt.payload["account_reference"], outgoing.payload["account_reference"])
            self.assertEqual(receipt.payload["currency_code"], outgoing.payload["currency_code"])
            ratio = Decimal(outgoing.payload["amount"]) / Decimal(receipt.payload["amount"])
            gap = outgoing.event_time - receipt.event_time
            if code == "same_account_below":
                self._assert_ratio(receipt, outgoing, "0.50")
                self.assertEqual(gap, timedelta(hours=12))
            elif code == "same_account_exact":
                self.assertEqual(ratio, Decimal("0.80"))
                self.assertEqual(gap, timedelta(hours=24))
                currencies.add(receipt.payload["currency_code"])
                customer_types.add(receipt.profile_code)
            elif code == "same_account_above":
                self._assert_ratio(receipt, outgoing, "0.95")
                self.assertGreater(ratio, Decimal("0.90"))
                self.assertEqual(gap, timedelta(hours=6))
            elif code == "same_account_late":
                self._assert_ratio(receipt, outgoing, "0.95")
                self.assertEqual(gap, timedelta(hours=36))
            elif code == "supplier_follow_on":
                self._assert_ratio(receipt, outgoing, "0.30")
                self.assertLess(ratio, Decimal("0.80"))
                self.assertEqual(gap, timedelta(hours=18))
                self.assertEqual(outgoing.payload["counterparty_name"], "Quarry Tools")
            elif code == "recurring_after_receipt":
                self._assert_ratio(receipt, outgoing, "0.10")
                self.assertLess(ratio, Decimal("0.80"))
                self.assertEqual(gap, timedelta(hours=8))
                self.assertEqual(outgoing.payload["counterparty_name"], "Lumen Freight")
        self.assertGreaterEqual(currencies, {"USD", "GBP"})
        self.assertTrue({"small_business", "cross_border"} <= customer_types)

    def _assert_ratio(self, receipt, outgoing, ratio: str) -> None:
        expected = (Decimal(receipt.payload["amount"]) * Decimal(ratio)).quantize(Decimal("0.01"))
        self.assertEqual(Decimal(outgoing.payload["amount"]), expected)

    def test_r002_alerts_only_the_same_account_shapes_that_meet_its_threshold(self) -> None:
        events = list(iter_events(WorkloadSpec(seed=11, population_id="shape-check", event_count=160)))
        rows, identity = _observed(events)
        episodes = find_turnover_episodes(rows, parse_turnover_parameters(R002_PARAMETERS))
        incoming_ids = {episode.incoming.transaction_id for episode in episodes}
        triggering_ids = {episode.triggering.transaction_id for episode in episodes}
        for code, (receipt, outgoing) in _pairs(events):
            receipt_id = identity[receipt.payload["source_event_id"]]
            outgoing_id = identity[outgoing.payload["source_event_id"]]
            if code in _QUALIFIES:
                self.assertIn(receipt_id, incoming_ids)
                self.assertIn(outgoing_id, triggering_ids)
            else:
                self.assertNotIn(receipt_id, incoming_ids)
                self.assertNotIn(outgoing_id, triggering_ids)
        for event in events:
            self.assertNotIn("scenario_code", event.payload)

    def test_split_payments_qualify_and_partial_payments_do_not(self) -> None:
        events = list(iter_events(WorkloadSpec(seed=11, population_id="shape-check", event_count=160)))
        rows, identity = _observed(events)
        episodes = find_turnover_episodes(rows, parse_turnover_parameters(R002_PARAMETERS))
        incoming = {episode.incoming.transaction_id: episode for episode in episodes}
        splits = _runs(events, "same_account_split", 4)
        partials = _runs(events, "same_account_partial", 3)
        self.assertTrue(splits)
        self.assertTrue(partials)
        ratios = set()
        for run in splits:
            receipt, *debits = run
            self.assertEqual(receipt.payload["direction"], "credit")
            account = receipt.payload["account_reference"]
            total = Decimal("0")
            for debit in debits:
                self.assertEqual(debit.payload["direction"], "debit")
                self.assertEqual(debit.payload["account_reference"], account)
                self.assertEqual(debit.payload["currency_code"], receipt.payload["currency_code"])
                ratio = Decimal(debit.payload["amount"]) / Decimal(receipt.payload["amount"])
                self.assertLess(ratio, Decimal("0.80"))
                total += ratio
                self.assertLessEqual(debit.event_time, receipt.event_time + timedelta(hours=24))
            self.assertGreaterEqual(total, Decimal("0.80"))
            ratios.add(
                tuple(Decimal(item.payload["amount"]) / Decimal(receipt.payload["amount"]) for item in debits)
            )
            episode = incoming[identity[receipt.payload["source_event_id"]]]
            self.assertEqual(episode.triggering.transaction_id, identity[debits[-1].payload["source_event_id"]])
            self.assertGreaterEqual(len(episode.outgoing), 2)
        self.assertGreater(len(ratios), 1)
        for run in partials:
            receipt = run[0]
            self.assertNotIn(identity[receipt.payload["source_event_id"]], incoming)
            paid = sum((Decimal(item.payload["amount"]) for item in run[1:]), Decimal("0"))
            self.assertLess(paid / Decimal(receipt.payload["amount"]), Decimal("0.80"))

    def test_velocity_shapes_follow_the_closed_one_hour_window(self) -> None:
        events = list(iter_events(WorkloadSpec(seed=11, population_id="shape-check", event_count=400)))
        bursts = find_velocity_bursts(_velocity_rows(events), parse_velocity_parameters(R001_PARAMETERS))
        triggering = {burst.triggering.transaction_id for burst in bursts}
        burst_runs = _runs(events, "velocity_burst", None)
        short_runs = _runs(events, "velocity_short", 5)
        wide_runs = _runs(events, "velocity_wide", 6)
        self.assertTrue(burst_runs and short_runs and wide_runs)
        plans = set()
        for run in burst_runs:
            self.assertIn(len(run), {6, 7, 8})
            plans.add(tuple(item.event_time - run[0].event_time for item in run))
            self.assertLessEqual(run[-1].event_time - run[0].event_time, timedelta(minutes=60))
            self.assertIn(_tx(run[5]), triggering)
            self.assertTrue(all(item.payload["direction"] == "debit" for item in run))
            self.assertEqual(len({item.payload["account_reference"] for item in run}), 1)
        self.assertGreater(len(plans), 1)
        for run in short_runs + wide_runs:
            for event in run:
                self.assertNotIn(_tx(event), triggering)

    def test_a_short_history_does_not_leave_a_receipt_without_its_payments(self) -> None:
        from datetime import datetime, timezone

        spec = WorkloadSpec(
            seed=11,
            population_id="boundary",
            event_count=300,
            history_end=datetime(2025, 1, 25, tzinfo=timezone.utc),
        )
        events = list(iter_events(spec))
        for code, length in {
            "same_account_exact": 2,
            "same_account_above": 2,
            "same_account_below": 2,
            "same_account_late": 2,
            "supplier_follow_on": 2,
            "recurring_after_receipt": 2,
            "cross_currency_outgoing": 2,
            "rapid_movement": 2,
            "same_account_split": 4,
            "same_account_partial": 3,
            "velocity_short": 5,
            "velocity_wide": 6,
        }.items():
            for run in _runs(events, code, length):
                self.assertEqual(len(run), length)
                self.assertLessEqual(run[-1].event_time, spec.history_end)
        for run in _runs(events, "velocity_burst", None):
            self.assertIn(len(run), {6, 7, 8})
            self.assertLessEqual(run[-1].event_time, spec.history_end)


def _runs(events, code: str, length: int | None) -> list[list]:
    """Group one designed episode. Adjacent episodes of the same code stay separate."""

    by_party: dict[str, list] = defaultdict(list)
    for event in events:
        party = event.payload["source_event_id"].split(":")[2]
        by_party[party].append(event)
    found: list[list] = []
    for party_events in by_party.values():
        ordered = sorted(party_events, key=lambda item: item.payload["source_event_id"])
        index = 0
        while index < len(ordered):
            if ordered[index].scenario_code != code:
                index += 1
                continue
            if length is None:
                size = 1
                while (
                    index + size < len(ordered)
                    and ordered[index + size].scenario_code == code
                    and ordered[index + size].event_time - ordered[index + size - 1].event_time
                    <= timedelta(minutes=30)
                ):
                    size += 1
            else:
                size = length
            run = ordered[index : index + size]
            if len(run) != size or any(item.scenario_code != code for item in run):
                raise AssertionError(
                    f"incomplete {code} episode at {ordered[index].payload['source_event_id']}"
                )
            found.append(run)
            index += size
    return found


def _tx(event) -> object:
    return uuid5(NAMESPACE_URL, event.payload["source_event_id"])


def _velocity_rows(events) -> list[ObservedTransaction]:
    rows = []
    for event in events:
        payload = event.payload
        rows.append(
            ObservedTransaction(
                transaction_id=uuid5(NAMESPACE_URL, payload["source_event_id"]),
                account_id=uuid5(NAMESPACE_URL, payload["account_reference"]),
                customer_id=uuid5(NAMESPACE_URL, payload["source_event_id"].split(":")[2]),
                direction=payload["direction"],
                event_time=event.event_time,
            )
        )
    rows.sort(key=lambda item: (item.account_id, item.event_time, item.transaction_id))
    return rows


def _pairs(events) -> list[tuple[str, tuple]]:
    by_party: dict[str, list] = defaultdict(list)
    for event in events:
        party = event.payload["source_event_id"].split(":")[2]
        by_party[party].append(event)
    found = []
    for party_events in by_party.values():
        ordered = sorted(party_events, key=lambda item: item.payload["source_event_id"])
        index = 0
        while index < len(ordered) - 1:
            code = ordered[index].scenario_code
            if (
                code in _PAIR_CODES | {"rapid_movement"}
                and ordered[index + 1].scenario_code == code
                and ordered[index].payload["direction"] == "credit"
                and ordered[index + 1].payload["direction"] == "debit"
            ):
                found.append((code, (ordered[index], ordered[index + 1])))
                index += 2
            else:
                index += 1
    return found


def _observed(events):
    identity = {}
    rows = []
    customers = {}
    for event in events:
        payload = event.payload
        account_id = uuid5(NAMESPACE_URL, payload["account_reference"])
        customers.setdefault(payload["account_reference"], uuid5(NAMESPACE_URL, payload["account_reference"] + ":customer"))
        transaction_id = uuid5(NAMESPACE_URL, payload["source_event_id"])
        identity[payload["source_event_id"]] = transaction_id
        rows.append(
            TurnoverTransaction(
                transaction_id=transaction_id,
                account_id=account_id,
                customer_id=customers[payload["account_reference"]],
                direction=payload["direction"],
                event_time=event.event_time,
                amount=Decimal(payload["amount"]),
                currency_code=payload["currency_code"],
            )
        )
    rows.sort(key=lambda item: (item.account_id, item.event_time, item.transaction_id))
    return rows, identity


if __name__ == "__main__":
    unittest.main()

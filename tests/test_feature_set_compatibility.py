"""Compatibility between a model version and the feature set used to score.

PostgreSQL is not executed here. The cases define the exact-match rule, and
the migration text is checked for the same comparisons.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0001_initial_schema.sql"
FUNCTION_RE = re.compile(
    r"CREATE OR REPLACE FUNCTION amlog\.enforce_score_snapshot\(\).*?\$\$.*?\$\$;",
    re.DOTALL,
)

# model name, model version, computation name, computation version,
# recorded name, recorded version, allowed
COMBINATIONS = (
    ("channel_behavior", "2026.01", "channel_behavior", "2026.01", "channel_behavior", "2026.01", True),
    ("payments", "1.0.0", "payments", "1.0.0", "payments", "1.0.0", True),
    ("channel_behavior", "2026.01", "channel_behavior", "2026.02", "channel_behavior", "2026.02", False),
    ("channel_behavior", "2026.01", "counterparty_behavior", "2026.01", "counterparty_behavior", "2026.01", False),
    ("channel_behavior", "1", "account_behavior", "2", "account_behavior", "2", False),
    ("Channel_Behavior", "2026.01", "channel_behavior", "2026.01", "channel_behavior", "2026.01", False),
    ("payments", "1", "payments", "1.0", "payments", "1.0", False),
    ("payments", "2026.01", "payments", "2026.01 ", "payments", "2026.01 ", False),
    ("channel_behavior", "2026.01", "channel_behavior", "2026.01", "channel_behavior", "2026.02", False),
    ("channel_behavior", "2026.01", "channel_behavior", "2026.02", "channel_behavior", "2026.01", False),
)

REQUIRED_SQL = (
    "model_versions.feature_set_name",
    "model_versions.feature_set_version",
    "model_feature_set_name IS DISTINCT FROM computation_feature_set_name",
    "model_feature_set_version IS DISTINCT FROM computation_feature_set_version",
    "model_feature_set_name IS DISTINCT FROM NEW.feature_set_name",
    "model_feature_set_version IS DISTINCT FROM NEW.feature_set_version",
    "computation_feature_set_name IS DISTINCT FROM NEW.feature_set_name",
    "computation_feature_set_version IS DISTINCT FROM NEW.feature_set_version",
    "risk score feature set must match the feature set required by the model version",
)


def is_distinct_from(left: str, right: str) -> bool:
    """PostgreSQL IS DISTINCT FROM for non-null text: exact, case-sensitive, untrimmed."""

    return left != right


def score_feature_set_is_allowed(
    model_feature_set_name: str,
    model_feature_set_version: str,
    computation_feature_set_name: str,
    computation_feature_set_version: str,
    recorded_feature_set_name: str,
    recorded_feature_set_version: str,
) -> bool:
    """Return whether enforce_score_snapshot would accept this feature-set trio."""

    mismatched = (
        is_distinct_from(computation_feature_set_name, recorded_feature_set_name)
        or is_distinct_from(computation_feature_set_version, recorded_feature_set_version)
        or is_distinct_from(model_feature_set_name, computation_feature_set_name)
        or is_distinct_from(model_feature_set_version, computation_feature_set_version)
        or is_distinct_from(model_feature_set_name, recorded_feature_set_name)
        or is_distinct_from(model_feature_set_version, recorded_feature_set_version)
    )
    return not mismatched


class FeatureSetCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        match = FUNCTION_RE.search(sql)
        if match is None:
            raise AssertionError("enforce_score_snapshot was not found in the migration.")
        cls.function_sql = match.group(0)

    def test_trigger_reads_the_model_feature_set_and_rejects_a_mismatch(self) -> None:
        for fragment in REQUIRED_SQL:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.function_sql)

    def test_valid_and_invalid_model_feature_combinations(self) -> None:
        self.test_trigger_reads_the_model_feature_set_and_rejects_a_mismatch()
        for (
            model_name,
            model_version,
            computation_name,
            computation_version,
            recorded_name,
            recorded_version,
            allowed,
        ) in COMBINATIONS:
            with self.subTest(
                model=f"{model_name}@{model_version}",
                computation=f"{computation_name}@{computation_version}",
                recorded=f"{recorded_name}@{recorded_version}",
            ):
                self.assertEqual(
                    allowed,
                    score_feature_set_is_allowed(
                        model_name,
                        model_version,
                        computation_name,
                        computation_version,
                        recorded_name,
                        recorded_version,
                    ),
                )


if __name__ == "__main__":
    unittest.main()

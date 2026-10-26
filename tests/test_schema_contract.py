"""Contract tests for the foundation schema.

These tests read the migration file and the design document. They do not
connect to PostgreSQL, load rows, or execute trigger functions.
"""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amlog import __version__  # noqa: E402
from amlog.db.migrate import split_sql  # noqa: E402

MIGRATION = ROOT / "db" / "migrations" / "0001_initial_schema.sql"
DESIGN = ROOT / "docs" / "database-design.md"
PROJECT = ROOT / "pyproject.toml"

EXPECTED_TABLES = {
    "amlog.schema_migrations",
    "amlog.customers",
    "amlog.source_identities",
    "amlog.accounts",
    "amlog.transactions",
    "amlog.monitoring_rules",
    "amlog.alerts",
    "amlog.alert_transactions",
    "amlog.feature_computations",
    "amlog.alert_features",
    "amlog.model_versions",
    "amlog.risk_scores",
    "amlog.investigation_cases",
    "amlog.investigation_notes",
    "amlog.audit_events",
    "amlog_label.alert_labels",
}

TABLE_RE = re.compile(
    r"CREATE TABLE\s+((?:amlog|amlog_label)\.(\w+))\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL,
)
COLUMN_RE = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s+"
    r"(?:UUID|TEXT|DATE|INTEGER|BOOLEAN|NUMERIC|CHAR|JSONB|TIMESTAMPTZ)\b",
    re.IGNORECASE | re.MULTILINE,
)
FORBIDDEN_COLUMN_TYPE = re.compile(
    r"(?im)^\s*[a-z_][a-z0-9_]*\s+"
    r"(double\s+precision|real|float4|float8|float|money|timestamp)\b"
)


def table_columns(sql: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for match in TABLE_RE.finditer(sql):
        qualified, unqualified, body = match.group(1), match.group(2), match.group(3)
        found[qualified] = [column.lower() for column in COLUMN_RE.findall(body)]
        found[unqualified] = found[qualified]
    return found


def document_sections(markdown: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^## (.+)\n", markdown))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[match.group(1).strip().lower()] = markdown[start:end]
    return sections


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = MIGRATION.read_bytes()
        cls.sql = cls.raw.decode("utf-8")
        cls.columns = table_columns(cls.sql)
        cls.statements = split_sql(cls.sql)

    def test_migration_file_is_utf8_with_unix_newlines(self) -> None:
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", self.raw)
        self.assertNotIn(b"\r", self.raw)

    def test_expected_tables_are_created_and_no_others(self) -> None:
        created = {match.group(1).lower() for match in TABLE_RE.finditer(self.sql)}
        self.assertEqual(created, EXPECTED_TABLES)

    def test_every_table_has_columns_and_the_design_document_names_them(self) -> None:
        sections = document_sections(DESIGN.read_text(encoding="utf-8"))
        for qualified in EXPECTED_TABLES:
            unqualified = qualified.split(".", 1)[1]
            columns = self.columns[qualified]
            self.assertGreater(len(columns), 0, qualified)
            self.assertIn(unqualified, sections, qualified)
            for column in columns:
                self.assertRegex(sections[unqualified], rf"\b{re.escape(column)}\b")

    def test_money_and_instants_use_exact_postgresql_types(self) -> None:
        self.assertIsNone(FORBIDDEN_COLUMN_TYPE.search(self.sql))
        self.assertIn("amount NUMERIC(38, 8)", self.sql)
        self.assertIn("base_amount NUMERIC(38, 8)", self.sql)
        self.assertIn("fx_rate NUMERIC(38, 18)", self.sql)
        self.assertIn("value_numeric NUMERIC", self.sql)
        self.assertIn("score NUMERIC NOT NULL", self.sql)
        self.assertNotIn("TIMESTAMP WITHOUT TIME ZONE", self.sql.upper())
        self.assertGreaterEqual(self.sql.upper().count("TIMESTAMPTZ"), 20)

    def test_source_events_are_unique_inside_each_source_system(self) -> None:
        self.assertIn("UNIQUE (source_system, source_event_id)", self.sql)

    def test_foreign_keys_restrict_deletes(self) -> None:
        references = list(re.finditer(r"REFERENCES\s+amlog(?:_label)?\.\w+", self.sql))
        self.assertGreaterEqual(len(references), 15)
        for match in references:
            window = self.sql[match.start() : match.start() + 180]
            self.assertIn("ON DELETE RESTRICT", window)
        self.assertNotIn("ON DELETE CASCADE", self.sql.upper())

    def test_labels_are_outside_the_operational_schema_and_scores_do_not_cite_them(self) -> None:
        self.assertIn("CREATE SCHEMA amlog_label", self.sql)
        self.assertIn("CREATE TABLE amlog_label.alert_labels", self.sql)
        score_statement = next(
            statement
            for statement in self.statements
            if "CREATE TABLE amlog.risk_scores" in statement
        )
        self.assertNotIn("amlog_label", score_statement)
        self.assertNotIn("alert_labels", score_statement)
        self.assertNotIn("label_value", score_statement)

    def test_rule_and_model_versions_keep_their_identity(self) -> None:
        self.assertIn("UNIQUE (rule_code, version_number)", self.sql)
        self.assertIn("UNIQUE (model_name, version_label)", self.sql)
        self.assertIn("training_data_provenance TEXT NOT NULL", self.sql)
        self.assertIn("recorded_rule_code", self.sql)
        self.assertIn("recorded_model_name", self.sql)
        self.assertIn("recorded_input_fingerprint", self.sql)

    def test_features_are_rows_rather_than_a_fixed_column_list(self) -> None:
        feature_body = next(
            match.group(3)
            for match in TABLE_RE.finditer(self.sql)
            if match.group(1) == "amlog.alert_features"
        )
        self.assertNotRegex(feature_body, r"feature_\d+")
        self.assertIn("feature_name", feature_body)
        self.assertIn("alert_features_value_shape_chk", self.sql)

    def test_operational_and_experiment_scores_are_distinguished(self) -> None:
        self.assertIn("score_use IN ('operational', 'experiment')", self.sql)
        self.assertIn("operational scores require an approved model version", self.sql)

    def test_append_only_history_is_triggered(self) -> None:
        for table in (
            "transactions",
            "alert_transactions",
            "feature_computations",
            "alert_features",
            "risk_scores",
            "audit_events",
            "investigation_notes",
            "alert_labels",
            "schema_migrations",
        ):
            self.assertIn(f"ON amlog.{table}" if table != "alert_labels" else "ON amlog_label.alert_labels", self.sql)
            self.assertIn(f"{table}_reject_", self.sql)

    def test_migration_has_no_seed_data_and_no_extension_install(self) -> None:
        for statement in self.statements:
            self.assertFalse(statement.lstrip().lower().startswith("insert"))
            self.assertEqual(statement.count("$$") % 2, 0)
        self.assertNotIn("CREATE EXTENSION", self.sql.upper())
        self.assertEqual(self.sql.lower().count("create table"), len(EXPECTED_TABLES))

    def test_project_metadata_declares_the_ranking_library_and_no_others(self) -> None:
        with PROJECT.open("rb") as handle:
            project = tomllib.load(handle)
        self.assertEqual(project["project"]["dependencies"], ["scikit-learn>=1.5,<1.8"])
        self.assertEqual(project["project"]["version"], __version__)
        text = PROJECT.read_text(encoding="utf-8").lower()
        for library in ("torch", "tensorflow", "xgboost", "pandas", "faker"):
            self.assertNotIn(library, text)
        self.assertIn("psycopg", text)
        self.assertIn("scikit-learn", text)


if __name__ == "__main__":
    unittest.main()

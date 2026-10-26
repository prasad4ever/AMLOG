"""Integration tests against a real PostgreSQL database.

Each test uses one transaction and rolls it back. Rows inserted here must not
remain after the test. The tests do not load a payment dataset.

They run only when AMLOG_DATABASE_URL is set and psycopg is installed.
Otherwise the module is skipped so the SQL contract tests can still run.
"""

from __future__ import annotations

import os
import unittest
import uuid
from decimal import Decimal

try:
    import psycopg
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None


EXPECTED_TABLES = {
    ("amlog", "schema_migrations"),
    ("amlog", "customers"),
    ("amlog", "source_identities"),
    ("amlog", "accounts"),
    ("amlog", "transactions"),
    ("amlog", "monitoring_rules"),
    ("amlog", "alerts"),
    ("amlog", "alert_transactions"),
    ("amlog", "feature_computations"),
    ("amlog", "alert_features"),
    ("amlog", "model_versions"),
    ("amlog", "risk_scores"),
    ("amlog", "investigation_cases"),
    ("amlog", "investigation_notes"),
    ("amlog", "audit_events"),
    ("amlog_label", "alert_labels"),
}


def database_url() -> str:
    return os.environ.get("AMLOG_DATABASE_URL", "").strip()


class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        url = database_url()
        if not url:
            raise unittest.SkipTest("AMLOG_DATABASE_URL is not set")
        cls.url = url

    def setUp(self) -> None:
        self.conn = psycopg.connect(self.url, connect_timeout=5)
        self.conn.autocommit = False

    def tearDown(self) -> None:
        try:
            self.conn.rollback()
        finally:
            self.conn.close()

    def test_server_version_and_expected_tables(self) -> None:
        version = self.conn.execute("SHOW server_version").fetchone()[0]
        self.assertTrue(version.startswith("16."), version)

        rows = self.conn.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema IN ('amlog', 'amlog_label')
            """
        ).fetchall()
        self.assertEqual(set(rows), EXPECTED_TABLES)

        applied = self.conn.execute(
            "SELECT version, name FROM amlog.schema_migrations"
        ).fetchall()
        self.assertEqual(applied, [("0001", "initial_schema")])

    def test_account_foreign_key_rejects_an_unknown_customer(self) -> None:
        with self.assertRaises(psycopg.errors.ForeignKeyViolation) as caught:
            with self.conn.transaction():
                self._insert_account(uuid.uuid4())
        self.assertEqual(caught.exception.diag.constraint_name, "accounts_customer_fk")

    def test_transaction_foreign_key_rejects_an_unknown_account(self) -> None:
        customer_id = self._insert_customer()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation) as caught:
            with self.conn.transaction():
                self._insert_transaction(uuid.uuid4(), customer_id, self._token())
        self.assertEqual(caught.exception.sqlstate, "23503")
        self.assertIn("does not exist", str(caught.exception))

    def test_alert_link_foreign_key_rejects_an_unknown_transaction(self) -> None:
        world = self._world()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation) as caught:
            with self.conn.transaction():
                self.conn.execute(
                    """
                    INSERT INTO amlog.alert_transactions (
                        alert_id, transaction_id, relationship_role
                    )
                    VALUES (%s, %s, 'triggering')
                    """,
                    (world["alert_id"], uuid.uuid4()),
                )
        self.assertEqual(
            caught.exception.diag.constraint_name,
            "alert_transactions_transaction_fk",
        )

    def test_duplicate_source_event_is_rejected(self) -> None:
        customer_id = self._insert_customer()
        account_id = self._insert_account(customer_id)
        event_id = self._token()
        self._insert_transaction(account_id, customer_id, event_id)

        with self.assertRaises(psycopg.errors.UniqueViolation) as caught:
            with self.conn.transaction():
                self._insert_transaction(account_id, customer_id, event_id)
        self.assertEqual(
            caught.exception.diag.constraint_name,
            "transactions_source_event_unique",
        )

        other = self._insert_transaction(account_id, customer_id, self._token())
        self.assertIsNotNone(other)

    def test_transaction_update_and_delete_are_rejected(self) -> None:
        customer_id = self._insert_customer()
        account_id = self._insert_account(customer_id)
        transaction_id = self._insert_transaction(account_id, customer_id, self._token())

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState) as update_error:
            with self.conn.transaction():
                self.conn.execute(
                    "UPDATE amlog.transactions SET amount = %s WHERE transaction_id = %s",
                    (Decimal("11.00"), transaction_id),
                )
        self.assertIn("amlog.transactions", str(update_error.exception))

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState) as delete_error:
            with self.conn.transaction():
                self.conn.execute(
                    "DELETE FROM amlog.transactions WHERE transaction_id = %s",
                    (transaction_id,),
                )
        self.assertIn("amlog.transactions", str(delete_error.exception))

        amount = self.conn.execute(
            "SELECT amount FROM amlog.transactions WHERE transaction_id = %s",
            (transaction_id,),
        ).fetchone()[0]
        self.assertEqual(amount, Decimal("10.00000000"))

    def test_operational_score_requires_an_approved_model(self) -> None:
        world = self._world()
        computation_id = self._insert_computation(world["alert_id"], "payments", "1")

        registered_id = self._insert_model("registered", "payments", "1", "reg-1")
        validated_id = self._insert_model("validated", "payments", "1", "val-1")
        rejected_id = self._insert_model("rejected", "payments", "1", "rej-1")
        approved_id = self._insert_model("approved", "payments", "1", "app-1")

        for model_id, label in (
            (registered_id, "reg-1"),
            (validated_id, "val-1"),
            (rejected_id, "rej-1"),
        ):
            with self.subTest(model=label, score_use="operational"):
                with self.assertRaises(psycopg.errors.CheckViolation) as caught:
                    with self.conn.transaction():
                        self._insert_score(
                            world["alert_id"],
                            model_id,
                            label,
                            computation_id,
                            "payments",
                            "1",
                            "operational",
                        )
                self.assertIn("approved model version", str(caught.exception))

        score_id = self._insert_score(
            world["alert_id"],
            approved_id,
            "app-1",
            computation_id,
            "payments",
            "1",
            "operational",
        )
        self.assertIsNotNone(score_id)

    def test_experiment_score_requires_a_validated_or_approved_model(self) -> None:
        world = self._world()
        computation_id = self._insert_computation(world["alert_id"], "payments", "1")
        registered_id = self._insert_model("registered", "payments", "1", "reg-exp")
        validated_id = self._insert_model("validated", "payments", "1", "val-exp")

        with self.assertRaises(psycopg.errors.CheckViolation) as caught:
            with self.conn.transaction():
                self._insert_score(
                    world["alert_id"],
                    registered_id,
                    "reg-exp",
                    computation_id,
                    "payments",
                    "1",
                    "experiment",
                )
        self.assertIn("validated or approved", str(caught.exception))

        experiment_score = self._insert_score(
            world["alert_id"],
            validated_id,
            "val-exp",
            computation_id,
            "payments",
            "1",
            "experiment",
        )
        self.assertIsNotNone(experiment_score)

    def test_feature_set_must_match_the_model_version(self) -> None:
        world = self._world()
        computation_id = self._insert_computation(world["alert_id"], "payments", "1")
        wrong_version_id = self._insert_model("approved", "payments", "2", "app-v2")
        wrong_name_id = self._insert_model("approved", "channels", "1", "app-name")
        matching_id = self._insert_model("approved", "payments", "1", "app-match")

        for model_id, label, feature_name, feature_version in (
            (wrong_version_id, "app-v2", "payments", "2"),
            (wrong_name_id, "app-name", "channels", "1"),
        ):
            with self.subTest(model=label):
                with self.assertRaises(psycopg.errors.CheckViolation) as caught:
                    with self.conn.transaction():
                        self._insert_score(
                            world["alert_id"],
                            model_id,
                            label,
                            computation_id,
                            "payments",
                            "1",
                            "operational",
                        )
                self.assertIn(
                    "feature set required by the model version",
                    str(caught.exception),
                )
                self.assertNotEqual((feature_name, feature_version), ("payments", "1"))

        matching_score = self._insert_score(
            world["alert_id"],
            matching_id,
            "app-match",
            computation_id,
            "payments",
            "1",
            "operational",
        )
        self.assertIsNotNone(matching_score)

    def test_uncommitted_rows_are_invisible_to_other_connections(self) -> None:
        customer_id = self._insert_customer()
        with psycopg.connect(self.url, connect_timeout=5) as other:
            other.autocommit = True
            found = other.execute(
                "SELECT 1 FROM amlog.customers WHERE customer_id = %s",
                (customer_id,),
            ).fetchone()
        self.assertIsNone(found)

    def _token(self) -> str:
        return uuid.uuid4().hex

    def _insert_customer(self) -> uuid.UUID:
        return self.conn.execute(
            """
            INSERT INTO amlog.customers (customer_reference, customer_type, legal_name)
            VALUES (%s, 'individual', 'Integration subject')
            RETURNING customer_id
            """,
            (self._token(),),
        ).fetchone()[0]

    def _insert_account(self, customer_id: uuid.UUID) -> uuid.UUID:
        token = self._token()
        return self.conn.execute(
            """
            INSERT INTO amlog.accounts (
                customer_id, account_reference, source_system, source_account_id,
                account_type, currency_code
            )
            VALUES (%s, %s, 'integration-test', %s, 'deposit', 'USD')
            RETURNING account_id
            """,
            (customer_id, token, token),
        ).fetchone()[0]

    def _insert_transaction(
        self,
        account_id: uuid.UUID,
        customer_id: uuid.UUID,
        source_event_id: str,
    ) -> uuid.UUID:
        return self.conn.execute(
            """
            INSERT INTO amlog.transactions (
                source_system, source_event_id, account_id, customer_id,
                payment_channel, transaction_type, direction, amount, currency_code,
                event_timestamp
            )
            VALUES (
                'integration-test', %s, %s, %s,
                'wire', 'transfer', 'debit', %s, 'USD',
                now()
            )
            RETURNING transaction_id
            """,
            (source_event_id, account_id, customer_id, Decimal("10.00")),
        ).fetchone()[0]

    def _insert_active_rule(self) -> tuple[uuid.UUID, str]:
        rule_code = "integration-" + self._token()
        rule_id = self.conn.execute(
            """
            INSERT INTO amlog.monitoring_rules (
                rule_code, version_number, rule_name, severity,
                lifecycle_status, effective_from, authored_by
            )
            VALUES (%s, 1, 'Integration rule', 'medium', 'active', now(), 'integration-test')
            RETURNING monitoring_rule_id
            """,
            (rule_code,),
        ).fetchone()[0]
        return rule_id, rule_code

    def _insert_alert(
        self,
        customer_id: uuid.UUID,
        rule_id: uuid.UUID,
        rule_code: str,
    ) -> uuid.UUID:
        return self.conn.execute(
            """
            INSERT INTO amlog.alerts (
                alert_reference, monitoring_rule_id, recorded_rule_code,
                recorded_rule_version, customer_id, triggered_at, severity
            )
            VALUES (%s, %s, %s, 1, %s, now(), 'medium')
            RETURNING alert_id
            """,
            (self._token(), rule_id, rule_code, customer_id),
        ).fetchone()[0]

    def _world(self) -> dict[str, uuid.UUID]:
        customer_id = self._insert_customer()
        rule_id, rule_code = self._insert_active_rule()
        alert_id = self._insert_alert(customer_id, rule_id, rule_code)
        return {"customer_id": customer_id, "alert_id": alert_id}

    def _insert_computation(
        self,
        alert_id: uuid.UUID,
        feature_set_name: str,
        feature_set_version: str,
    ) -> uuid.UUID:
        return self.conn.execute(
            """
            INSERT INTO amlog.feature_computations (
                alert_id, feature_set_name, feature_set_version,
                input_fingerprint, computed_at
            )
            VALUES (%s, %s, %s, %s, now())
            RETURNING feature_computation_id
            """,
            (alert_id, feature_set_name, feature_set_version, "integration-" + self._token()),
        ).fetchone()[0]

    def _insert_model(
        self,
        lifecycle_status: str,
        feature_set_name: str,
        feature_set_version: str,
        version_label: str,
    ) -> uuid.UUID:
        approved = lifecycle_status == "approved"
        return self.conn.execute(
            """
            INSERT INTO amlog.model_versions (
                model_name, version_label, feature_set_name, feature_set_version,
                training_data_provenance, lifecycle_status, registered_by,
                approved_at, approved_by
            )
            VALUES (
                %s, %s, %s, %s,
                'synthetic integration fixture; no operational records',
                %s, 'integration-test',
                CASE WHEN %s THEN now() ELSE NULL END,
                CASE WHEN %s THEN 'integration-test' ELSE NULL END
            )
            RETURNING model_version_id
            """,
            (
                "integration-model-" + self._token(),
                version_label,
                feature_set_name,
                feature_set_version,
                lifecycle_status,
                approved,
                approved,
            ),
        ).fetchone()[0]

    def _insert_score(
        self,
        alert_id: uuid.UUID,
        model_version_id: uuid.UUID,
        version_label: str,
        computation_id: uuid.UUID,
        feature_set_name: str,
        feature_set_version: str,
        score_use: str,
    ) -> uuid.UUID:
        model_name = self.conn.execute(
            "SELECT model_name FROM amlog.model_versions WHERE model_version_id = %s",
            (model_version_id,),
        ).fetchone()[0]
        fingerprint = self.conn.execute(
            """
            SELECT input_fingerprint
            FROM amlog.feature_computations
            WHERE feature_computation_id = %s
            """,
            (computation_id,),
        ).fetchone()[0]
        return self.conn.execute(
            """
            INSERT INTO amlog.risk_scores (
                alert_id, model_version_id, recorded_model_name, recorded_version_label,
                feature_set_name, feature_set_version, feature_computation_id,
                recorded_input_fingerprint, score, score_semantics, score_use,
                scoring_run_id, scored_at
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, 'integration score; higher means more urgent', %s,
                %s, now()
            )
            RETURNING risk_score_id
            """,
            (
                alert_id,
                model_version_id,
                model_name,
                version_label,
                feature_set_name,
                feature_set_version,
                computation_id,
                fingerprint,
                Decimal("0.5"),
                score_use,
                uuid.uuid4(),
            ),
        ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()

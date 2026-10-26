-- AMLOG foundation schema, migration 0001.
-- Baseline server: PostgreSQL 15 or newer.
--
-- This script creates structures only. It does not insert customers,
-- accounts, transactions, rules, models, scores, labels, or cases.
-- It does not define an executable monitoring rule and it does not
-- register a machine-learning model.
--
-- Money is NUMERIC, never floating point and never the locale-dependent
-- MONEY type. Instants are TIMESTAMPTZ. Source events are unique per
-- source system so a repeated delivery can be rejected.

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------

CREATE SCHEMA amlog;
CREATE SCHEMA amlog_label;

COMMENT ON SCHEMA amlog IS
  'Operational data for alert optimization. This schema does not store training or evaluation labels.';

COMMENT ON SCHEMA amlog_label IS
  'Ground-truth and evaluation labels. Scoring workloads must not be granted access to this schema.';

REVOKE ALL ON SCHEMA amlog FROM PUBLIC;
REVOKE ALL ON SCHEMA amlog_label FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Shared trigger functions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION amlog.reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION amlog.reject_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.touch_updated_at() FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.schema_migrations (
  version TEXT NOT NULL,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT schema_migrations_pkey PRIMARY KEY (version),
  CONSTRAINT schema_migrations_version_chk CHECK (version ~ '^[0-9]{4}$'),
  CONSTRAINT schema_migrations_name_chk CHECK (name ~ '^[a-z0-9_]+$'),
  CONSTRAINT schema_migrations_checksum_chk CHECK (checksum ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE amlog.schema_migrations IS
  'Versions of this database structure that have been applied. Rows are inserted by the migration runner.';

-- ---------------------------------------------------------------------------
-- Parties, external identities, and accounts
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.customers (
  customer_id UUID NOT NULL DEFAULT gen_random_uuid(),
  customer_reference TEXT NOT NULL,
  customer_type TEXT NOT NULL,
  legal_name TEXT NOT NULL,
  residency_country_code CHAR(2),
  lifecycle_status TEXT NOT NULL DEFAULT 'active',
  onboarded_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT customers_pkey PRIMARY KEY (customer_id),
  CONSTRAINT customers_reference_unique UNIQUE (customer_reference),
  CONSTRAINT customers_type_chk CHECK (customer_type IN ('individual', 'organization', 'other')),
  CONSTRAINT customers_status_chk CHECK (
    lifecycle_status IN ('prospect', 'active', 'restricted', 'closed')
  ),
  CONSTRAINT customers_country_chk CHECK (
    residency_country_code IS NULL OR residency_country_code ~ '^[A-Z]{2}$'
  ),
  CONSTRAINT customers_reference_chk CHECK (
    length(btrim(customer_reference)) > 0
    AND customer_reference = btrim(customer_reference)
  ),
  CONSTRAINT customers_legal_name_chk CHECK (length(btrim(legal_name)) > 0)
);

COMMENT ON TABLE amlog.customers IS
  'Monitoring subject. Government identifiers, birth dates, addresses, and contact details are intentionally omitted.';

CREATE TABLE amlog.source_identities (
  source_identity_id UUID NOT NULL DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL,
  source_system TEXT NOT NULL,
  identity_type TEXT NOT NULL,
  identity_value TEXT NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_to TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT source_identities_pkey PRIMARY KEY (source_identity_id),
  CONSTRAINT source_identities_customer_fk
    FOREIGN KEY (customer_id) REFERENCES amlog.customers (customer_id) ON DELETE RESTRICT,
  CONSTRAINT source_identities_validity_chk CHECK (valid_to IS NULL OR valid_to > valid_from),
  CONSTRAINT source_identities_system_chk CHECK (
    length(btrim(source_system)) > 0 AND source_system = btrim(source_system)
  ),
  CONSTRAINT source_identities_type_chk CHECK (
    length(btrim(identity_type)) > 0 AND identity_type = btrim(identity_type)
  ),
  CONSTRAINT source_identities_value_chk CHECK (length(btrim(identity_value)) > 0)
);

COMMENT ON TABLE amlog.source_identities IS
  'Identifier of a customer in an upstream system. One open identity is allowed for each source, type, and value.';

CREATE UNIQUE INDEX source_identities_one_open_identity_idx
  ON amlog.source_identities (source_system, identity_type, identity_value)
  WHERE valid_to IS NULL;

CREATE TABLE amlog.accounts (
  account_id UUID NOT NULL DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL,
  account_reference TEXT NOT NULL,
  source_system TEXT NOT NULL,
  source_account_id TEXT NOT NULL,
  account_type TEXT NOT NULL,
  currency_code CHAR(3) NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'open',
  opened_at TIMESTAMPTZ,
  closed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT accounts_pkey PRIMARY KEY (account_id),
  CONSTRAINT accounts_customer_fk
    FOREIGN KEY (customer_id) REFERENCES amlog.customers (customer_id) ON DELETE RESTRICT,
  CONSTRAINT accounts_reference_unique UNIQUE (account_reference),
  CONSTRAINT accounts_source_unique UNIQUE (source_system, source_account_id),
  CONSTRAINT accounts_reference_chk CHECK (
    length(btrim(account_reference)) > 0 AND account_reference = btrim(account_reference)
  ),
  CONSTRAINT accounts_source_chk CHECK (
    length(btrim(source_system)) > 0
    AND source_system = btrim(source_system)
    AND length(btrim(source_account_id)) > 0
  ),
  CONSTRAINT accounts_type_chk CHECK (
    length(btrim(account_type)) > 0 AND account_type = btrim(account_type)
  ),
  CONSTRAINT accounts_currency_chk CHECK (currency_code ~ '^[A-Z]{3}$'),
  CONSTRAINT accounts_status_chk CHECK (
    lifecycle_status IN ('pending', 'open', 'restricted', 'closed')
  ),
  CONSTRAINT accounts_dates_chk CHECK (
    closed_at IS NULL OR opened_at IS NULL OR closed_at >= opened_at
  )
);

COMMENT ON TABLE amlog.accounts IS
  'Account held by one current customer. Joint ownership and ownership history are not modeled yet.';

-- ---------------------------------------------------------------------------
-- Financial events
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.transactions (
  transaction_id UUID NOT NULL DEFAULT gen_random_uuid(),
  source_system TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  account_id UUID NOT NULL,
  customer_id UUID NOT NULL,
  counterparty_reference TEXT,
  counterparty_name TEXT,
  counterparty_country_code CHAR(2),
  counterparty_institution_reference TEXT,
  payment_channel TEXT NOT NULL,
  transaction_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  amount NUMERIC(38, 8) NOT NULL,
  currency_code CHAR(3) NOT NULL,
  base_currency_code CHAR(3),
  base_amount NUMERIC(38, 8),
  fx_rate NUMERIC(38, 18),
  event_timestamp TIMESTAMPTZ NOT NULL,
  booked_at TIMESTAMPTZ,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  corrects_transaction_id UUID,
  correlation_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT transactions_pkey PRIMARY KEY (transaction_id),
  CONSTRAINT transactions_account_fk
    FOREIGN KEY (account_id) REFERENCES amlog.accounts (account_id) ON DELETE RESTRICT,
  CONSTRAINT transactions_customer_fk
    FOREIGN KEY (customer_id) REFERENCES amlog.customers (customer_id) ON DELETE RESTRICT,
  CONSTRAINT transactions_source_event_unique UNIQUE (source_system, source_event_id),
  CONSTRAINT transactions_source_chk CHECK (
    length(btrim(source_system)) > 0
    AND source_system = btrim(source_system)
    AND length(btrim(source_event_id)) > 0
  ),
  CONSTRAINT transactions_channel_chk CHECK (
    length(btrim(payment_channel)) > 0 AND payment_channel = btrim(payment_channel)
  ),
  CONSTRAINT transactions_type_chk CHECK (
    length(btrim(transaction_type)) > 0 AND transaction_type = btrim(transaction_type)
  ),
  CONSTRAINT transactions_direction_chk CHECK (direction IN ('debit', 'credit')),
  CONSTRAINT transactions_amount_chk CHECK (amount >= 0),
  CONSTRAINT transactions_currency_chk CHECK (currency_code ~ '^[A-Z]{3}$'),
  CONSTRAINT transactions_base_currency_chk CHECK (
    base_currency_code IS NULL OR base_currency_code ~ '^[A-Z]{3}$'
  ),
  CONSTRAINT transactions_base_amount_chk CHECK (base_amount IS NULL OR base_amount >= 0),
  CONSTRAINT transactions_base_pair_chk CHECK (
    (base_currency_code IS NULL AND base_amount IS NULL)
    OR (base_currency_code IS NOT NULL AND base_amount IS NOT NULL)
  ),
  CONSTRAINT transactions_fx_chk CHECK (
    fx_rate IS NULL
    OR (
      fx_rate > 0
      AND base_currency_code IS NOT NULL
      AND base_amount IS NOT NULL
    )
  ),
  CONSTRAINT transactions_counterparty_country_chk CHECK (
    counterparty_country_code IS NULL OR counterparty_country_code ~ '^[A-Z]{2}$'
  ),
  CONSTRAINT transactions_not_self_correction_chk CHECK (
    corrects_transaction_id IS NULL OR corrects_transaction_id <> transaction_id
  )
);

ALTER TABLE amlog.transactions
  ADD CONSTRAINT transactions_corrects_fk
  FOREIGN KEY (corrects_transaction_id) REFERENCES amlog.transactions (transaction_id)
  ON DELETE RESTRICT;

COMMENT ON TABLE amlog.transactions IS
  'Immutable financial event as received from a source. A repeated source event identifier is rejected.';

COMMENT ON COLUMN amlog.transactions.amount IS
  'Exact non-negative amount in currency_code. Direction says whether it is a debit or a credit.';

COMMENT ON COLUMN amlog.transactions.source_event_id IS
  'Identifier assigned by the source system. Together with source_system it prevents duplicate events.';

COMMENT ON COLUMN amlog.transactions.customer_id IS
  'Customer recorded for this event. The account-owner trigger requires the account current owner.';

COMMENT ON COLUMN amlog.transactions.payment_channel IS
  'Source channel name. The schema does not limit which channels may exist.';

-- ---------------------------------------------------------------------------
-- Rule versions and investigation cases
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.monitoring_rules (
  monitoring_rule_id UUID NOT NULL DEFAULT gen_random_uuid(),
  rule_code TEXT NOT NULL,
  version_number INTEGER NOT NULL,
  rule_name TEXT NOT NULL,
  description TEXT,
  parameter_document JSONB NOT NULL DEFAULT '{}'::jsonb,
  severity TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'draft',
  effective_from TIMESTAMPTZ,
  effective_to TIMESTAMPTZ,
  authored_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT monitoring_rules_pkey PRIMARY KEY (monitoring_rule_id),
  CONSTRAINT monitoring_rules_version_unique UNIQUE (rule_code, version_number),
  CONSTRAINT monitoring_rules_version_positive_chk CHECK (version_number >= 1),
  CONSTRAINT monitoring_rules_code_chk CHECK (
    length(btrim(rule_code)) > 0 AND rule_code = btrim(rule_code)
  ),
  CONSTRAINT monitoring_rules_name_chk CHECK (length(btrim(rule_name)) > 0),
  CONSTRAINT monitoring_rules_author_chk CHECK (length(btrim(authored_by)) > 0),
  CONSTRAINT monitoring_rules_severity_chk CHECK (
    severity IN ('low', 'medium', 'high', 'critical')
  ),
  CONSTRAINT monitoring_rules_status_chk CHECK (
    lifecycle_status IN ('draft', 'active', 'retired')
  ),
  CONSTRAINT monitoring_rules_effective_from_chk CHECK (
    lifecycle_status = 'draft' OR effective_from IS NOT NULL
  ),
  CONSTRAINT monitoring_rules_effective_window_chk CHECK (
    effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from
  ),
  CONSTRAINT monitoring_rules_parameters_object_chk CHECK (
    jsonb_typeof(parameter_document) = 'object'
  )
);

COMMENT ON TABLE amlog.monitoring_rules IS
  'Saved rule versions. Publishing freezes the version. A change is a new row, not an overwrite. No rule is installed by this migration.';

COMMENT ON COLUMN amlog.monitoring_rules.parameter_document IS
  'Versioned parameter document reserved for a future rule definition. This foundation does not execute it.';

CREATE TABLE amlog.investigation_cases (
  investigation_case_id UUID NOT NULL DEFAULT gen_random_uuid(),
  case_reference TEXT NOT NULL,
  customer_id UUID NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'open',
  priority TEXT NOT NULL DEFAULT 'routine',
  disposition TEXT,
  assigned_to TEXT,
  status_summary TEXT,
  opened_at TIMESTAMPTZ NOT NULL,
  closed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT investigation_cases_pkey PRIMARY KEY (investigation_case_id),
  CONSTRAINT investigation_cases_customer_fk
    FOREIGN KEY (customer_id) REFERENCES amlog.customers (customer_id) ON DELETE RESTRICT,
  CONSTRAINT investigation_cases_reference_unique UNIQUE (case_reference),
  CONSTRAINT investigation_cases_reference_chk CHECK (
    length(btrim(case_reference)) > 0 AND case_reference = btrim(case_reference)
  ),
  CONSTRAINT investigation_cases_status_chk CHECK (
    lifecycle_status IN ('open', 'in_progress', 'pending_information', 'closed')
  ),
  CONSTRAINT investigation_cases_priority_chk CHECK (
    priority IN ('routine', 'elevated', 'urgent')
  ),
  CONSTRAINT investigation_cases_disposition_chk CHECK (
    disposition IS NULL
    OR disposition IN (
      'no_further_action',
      'continue_monitoring',
      'escalated',
      'inconclusive'
    )
  ),
  CONSTRAINT investigation_cases_closed_state_chk CHECK (
    (
      lifecycle_status = 'closed'
      AND disposition IS NOT NULL
      AND closed_at IS NOT NULL
    )
    OR (
      lifecycle_status <> 'closed'
      AND disposition IS NULL
      AND closed_at IS NULL
    )
  ),
  CONSTRAINT investigation_cases_dates_chk CHECK (
    closed_at IS NULL OR closed_at >= opened_at
  )
);

COMMENT ON TABLE amlog.investigation_cases IS
  'Human investigation record. Disposition is an operational outcome, not a model training label.';

COMMENT ON COLUMN amlog.investigation_cases.disposition IS
  'Workflow outcome recorded when the case is closed. It is not copied into scoring inputs.';

COMMENT ON COLUMN amlog.investigation_cases.status_summary IS
  'Current short status only. Investigator evidence belongs in investigation_notes, which cannot be rewritten.';

CREATE TABLE amlog.investigation_notes (
  investigation_note_id UUID NOT NULL DEFAULT gen_random_uuid(),
  investigation_case_id UUID NOT NULL,
  author_reference TEXT NOT NULL,
  note_text TEXT NOT NULL,
  noted_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT investigation_notes_pkey PRIMARY KEY (investigation_note_id),
  CONSTRAINT investigation_notes_case_fk
    FOREIGN KEY (investigation_case_id)
    REFERENCES amlog.investigation_cases (investigation_case_id) ON DELETE RESTRICT,
  CONSTRAINT investigation_notes_author_chk CHECK (length(btrim(author_reference)) > 0),
  CONSTRAINT investigation_notes_text_chk CHECK (length(btrim(note_text)) > 0)
);

COMMENT ON TABLE amlog.investigation_notes IS
  'Append-only investigator notes. A correction is a new note. The current case summary is not the evidence record.';

-- ---------------------------------------------------------------------------
-- Alerts and the transactions that support them
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.alerts (
  alert_id UUID NOT NULL DEFAULT gen_random_uuid(),
  alert_reference TEXT NOT NULL,
  monitoring_rule_id UUID NOT NULL,
  recorded_rule_code TEXT NOT NULL,
  recorded_rule_version INTEGER NOT NULL,
  customer_id UUID NOT NULL,
  account_id UUID,
  investigation_case_id UUID,
  triggered_at TIMESTAMPTZ NOT NULL,
  window_started_at TIMESTAMPTZ,
  window_ended_at TIMESTAMPTZ,
  severity TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'open',
  summary TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alerts_pkey PRIMARY KEY (alert_id),
  CONSTRAINT alerts_rule_fk
    FOREIGN KEY (monitoring_rule_id)
    REFERENCES amlog.monitoring_rules (monitoring_rule_id) ON DELETE RESTRICT,
  CONSTRAINT alerts_customer_fk
    FOREIGN KEY (customer_id) REFERENCES amlog.customers (customer_id) ON DELETE RESTRICT,
  CONSTRAINT alerts_account_fk
    FOREIGN KEY (account_id) REFERENCES amlog.accounts (account_id) ON DELETE RESTRICT,
  CONSTRAINT alerts_case_fk
    FOREIGN KEY (investigation_case_id)
    REFERENCES amlog.investigation_cases (investigation_case_id) ON DELETE RESTRICT,
  CONSTRAINT alerts_reference_unique UNIQUE (alert_reference),
  CONSTRAINT alerts_reference_chk CHECK (
    length(btrim(alert_reference)) > 0 AND alert_reference = btrim(alert_reference)
  ),
  CONSTRAINT alerts_recorded_rule_chk CHECK (
    length(btrim(recorded_rule_code)) > 0 AND recorded_rule_version >= 1
  ),
  CONSTRAINT alerts_severity_chk CHECK (severity IN ('low', 'medium', 'high', 'critical')),
  CONSTRAINT alerts_status_chk CHECK (
    lifecycle_status IN ('open', 'queued', 'in_review', 'closed', 'suppressed')
  ),
  CONSTRAINT alerts_window_chk CHECK (
    window_ended_at IS NULL
    OR window_started_at IS NULL
    OR window_ended_at >= window_started_at
  )
);

COMMENT ON TABLE amlog.alerts IS
  'A monitoring result asking for human attention. The cited rule version and generation snapshot cannot be rewritten.';

CREATE TABLE amlog.alert_transactions (
  alert_id UUID NOT NULL,
  transaction_id UUID NOT NULL,
  relationship_role TEXT NOT NULL,
  linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alert_transactions_pkey PRIMARY KEY (alert_id, transaction_id),
  CONSTRAINT alert_transactions_alert_fk
    FOREIGN KEY (alert_id) REFERENCES amlog.alerts (alert_id) ON DELETE RESTRICT,
  CONSTRAINT alert_transactions_transaction_fk
    FOREIGN KEY (transaction_id) REFERENCES amlog.transactions (transaction_id) ON DELETE RESTRICT,
  CONSTRAINT alert_transactions_role_chk CHECK (
    relationship_role IN ('triggering', 'supporting', 'contextual')
  )
);

COMMENT ON TABLE amlog.alert_transactions IS
  'Transactions cited by an alert. Links are retained rather than silently removed.';

-- ---------------------------------------------------------------------------
-- Feature snapshots
-- One computation holds many feature rows, so the number of features is not fixed.
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.feature_computations (
  feature_computation_id UUID NOT NULL DEFAULT gen_random_uuid(),
  alert_id UUID NOT NULL,
  feature_set_name TEXT NOT NULL,
  feature_set_version TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL,
  computed_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT feature_computations_pkey PRIMARY KEY (feature_computation_id),
  CONSTRAINT feature_computations_alert_fk
    FOREIGN KEY (alert_id) REFERENCES amlog.alerts (alert_id) ON DELETE RESTRICT,
  CONSTRAINT feature_computations_alert_computation_unique
    UNIQUE (feature_computation_id, alert_id),
  CONSTRAINT feature_computations_name_chk CHECK (
    length(btrim(feature_set_name)) > 0 AND feature_set_name = btrim(feature_set_name)
  ),
  CONSTRAINT feature_computations_version_chk CHECK (
    length(btrim(feature_set_version)) > 0
    AND feature_set_version = btrim(feature_set_version)
  ),
  CONSTRAINT feature_computations_fingerprint_chk CHECK (length(btrim(input_fingerprint)) > 0)
);

COMMENT ON TABLE amlog.feature_computations IS
  'Header for one immutable feature snapshot of one alert. The composite unique key lets scores reference that exact snapshot.';

COMMENT ON COLUMN amlog.feature_computations.input_fingerprint IS
  'Fingerprint of the inputs used for this snapshot. A later score must record the same fingerprint.';

CREATE TABLE amlog.alert_features (
  alert_feature_id UUID NOT NULL DEFAULT gen_random_uuid(),
  feature_computation_id UUID NOT NULL,
  alert_id UUID NOT NULL,
  feature_name TEXT NOT NULL,
  value_type TEXT NOT NULL,
  value_numeric NUMERIC,
  value_text TEXT,
  value_boolean BOOLEAN,
  value_timestamp TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alert_features_pkey PRIMARY KEY (alert_feature_id),
  CONSTRAINT alert_features_computation_fk
    FOREIGN KEY (feature_computation_id, alert_id)
    REFERENCES amlog.feature_computations (feature_computation_id, alert_id)
    ON DELETE RESTRICT,
  CONSTRAINT alert_features_identity_unique UNIQUE (
    feature_computation_id, feature_name
  ),
  CONSTRAINT alert_features_name_chk CHECK (
    length(btrim(feature_name)) > 0 AND feature_name = btrim(feature_name)
  ),
  CONSTRAINT alert_features_value_type_chk CHECK (
    value_type IN ('numeric', 'text', 'boolean', 'timestamp')
  ),
  CONSTRAINT alert_features_value_shape_chk CHECK (
    (
      value_type = 'numeric'
      AND value_numeric IS NOT NULL
      AND value_text IS NULL
      AND value_boolean IS NULL
      AND value_timestamp IS NULL
    )
    OR (
      value_type = 'text'
      AND value_text IS NOT NULL
      AND value_numeric IS NULL
      AND value_boolean IS NULL
      AND value_timestamp IS NULL
    )
    OR (
      value_type = 'boolean'
      AND value_boolean IS NOT NULL
      AND value_numeric IS NULL
      AND value_text IS NULL
      AND value_timestamp IS NULL
    )
    OR (
      value_type = 'timestamp'
      AND value_timestamp IS NOT NULL
      AND value_numeric IS NULL
      AND value_text IS NULL
      AND value_boolean IS NULL
    )
  )
);

COMMENT ON TABLE amlog.alert_features IS
  'One row per feature value. New behavior measures are new rows, not new columns and not a fixed limit.';

COMMENT ON COLUMN amlog.alert_features.value_numeric IS
  'Exact numeric feature value with no declared precision cap. Monetary event amounts use NUMERIC(38, 8) instead.';

-- ---------------------------------------------------------------------------
-- Model registry and scores
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.model_versions (
  model_version_id UUID NOT NULL DEFAULT gen_random_uuid(),
  model_name TEXT NOT NULL,
  version_label TEXT NOT NULL,
  algorithm_family TEXT,
  feature_set_name TEXT NOT NULL,
  feature_set_version TEXT NOT NULL,
  training_data_provenance TEXT NOT NULL,
  artifact_reference TEXT,
  evaluation_summary JSONB,
  lifecycle_status TEXT NOT NULL DEFAULT 'registered',
  registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  registered_by TEXT NOT NULL,
  approved_at TIMESTAMPTZ,
  approved_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT model_versions_pkey PRIMARY KEY (model_version_id),
  CONSTRAINT model_versions_natural_key UNIQUE (model_name, version_label),
  CONSTRAINT model_versions_name_chk CHECK (
    length(btrim(model_name)) > 0 AND model_name = btrim(model_name)
  ),
  CONSTRAINT model_versions_label_chk CHECK (
    length(btrim(version_label)) > 0 AND version_label = btrim(version_label)
  ),
  CONSTRAINT model_versions_feature_set_chk CHECK (
    length(btrim(feature_set_name)) > 0
    AND feature_set_name = btrim(feature_set_name)
    AND length(btrim(feature_set_version)) > 0
    AND feature_set_version = btrim(feature_set_version)
  ),
  CONSTRAINT model_versions_provenance_chk CHECK (length(btrim(training_data_provenance)) > 0),
  CONSTRAINT model_versions_registrant_chk CHECK (length(btrim(registered_by)) > 0),
  CONSTRAINT model_versions_status_chk CHECK (
    lifecycle_status IN ('registered', 'validated', 'approved', 'retired', 'rejected')
  ),
  CONSTRAINT model_versions_approval_pair_chk CHECK (
    (approved_at IS NULL AND approved_by IS NULL)
    OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)
  ),
  CONSTRAINT model_versions_evaluation_chk CHECK (
    evaluation_summary IS NULL OR jsonb_typeof(evaluation_summary) = 'object'
  )
);

COMMENT ON TABLE amlog.model_versions IS
  'Registry of model versions. No algorithm is selected or registered by this migration. Provenance is mandatory and, after insert, immutable.';

COMMENT ON COLUMN amlog.model_versions.algorithm_family IS
  'Optional family name recorded only when a future version is registered. Null does not imply an algorithm choice.';

COMMENT ON COLUMN amlog.model_versions.training_data_provenance IS
  'Required statement of where the training data came from, including whether it was synthetic or operational.';

CREATE TABLE amlog.risk_scores (
  risk_score_id UUID NOT NULL DEFAULT gen_random_uuid(),
  alert_id UUID NOT NULL,
  model_version_id UUID NOT NULL,
  recorded_model_name TEXT NOT NULL,
  recorded_version_label TEXT NOT NULL,
  recorded_algorithm_family TEXT,
  feature_set_name TEXT NOT NULL,
  feature_set_version TEXT NOT NULL,
  feature_computation_id UUID NOT NULL,
  recorded_input_fingerprint TEXT NOT NULL,
  score NUMERIC NOT NULL,
  score_semantics TEXT NOT NULL,
  score_use TEXT NOT NULL,
  explanation_document JSONB,
  scoring_run_id UUID NOT NULL,
  scored_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT risk_scores_pkey PRIMARY KEY (risk_score_id),
  CONSTRAINT risk_scores_alert_fk
    FOREIGN KEY (alert_id) REFERENCES amlog.alerts (alert_id) ON DELETE RESTRICT,
  CONSTRAINT risk_scores_model_fk
    FOREIGN KEY (model_version_id)
    REFERENCES amlog.model_versions (model_version_id) ON DELETE RESTRICT,
  CONSTRAINT risk_scores_computation_fk
    FOREIGN KEY (feature_computation_id, alert_id)
    REFERENCES amlog.feature_computations (feature_computation_id, alert_id)
    ON DELETE RESTRICT,
  CONSTRAINT risk_scores_semantics_chk CHECK (length(btrim(score_semantics)) > 0),
  CONSTRAINT risk_scores_fingerprint_chk CHECK (length(btrim(recorded_input_fingerprint)) > 0),
  CONSTRAINT risk_scores_use_chk CHECK (score_use IN ('operational', 'experiment')),
  CONSTRAINT risk_scores_explanation_chk CHECK (
    explanation_document IS NULL OR jsonb_typeof(explanation_document) = 'object'
  )
);

COMMENT ON TABLE amlog.risk_scores IS
  'Score produced for one alert by one model version using one feature snapshot. This table contains no ground-truth label.';

COMMENT ON COLUMN amlog.risk_scores.score IS
  'Exact score as defined by score_semantics. The schema does not force a 0 to 1 range because no algorithm is selected.';

COMMENT ON COLUMN amlog.risk_scores.score_semantics IS
  'Words recorded at scoring time that say what the number means and how it should be ordered.';

COMMENT ON COLUMN amlog.risk_scores.explanation_document IS
  'Optional explanation payload. The method is not prescribed. An empty explanation is stored as NULL, not as a fake document.';

-- ---------------------------------------------------------------------------
-- Audit
-- ---------------------------------------------------------------------------

CREATE TABLE amlog.audit_events (
  audit_event_id UUID NOT NULL DEFAULT gen_random_uuid(),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_type TEXT NOT NULL,
  actor_reference TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_schema TEXT NOT NULL,
  entity_table TEXT NOT NULL,
  entity_id UUID,
  correlation_id UUID,
  request_reference TEXT,
  previous_document JSONB,
  resulting_document JSONB,
  context_document JSONB,
  CONSTRAINT audit_events_pkey PRIMARY KEY (audit_event_id),
  CONSTRAINT audit_events_actor_type_chk CHECK (
    actor_type IN ('person', 'system', 'model', 'batch')
  ),
  CONSTRAINT audit_events_actor_chk CHECK (length(btrim(actor_reference)) > 0),
  CONSTRAINT audit_events_action_chk CHECK (length(btrim(action)) > 0),
  CONSTRAINT audit_events_entity_chk CHECK (
    entity_schema IN ('amlog', 'amlog_label')
    AND length(btrim(entity_table)) > 0
  ),
  CONSTRAINT audit_events_previous_chk CHECK (
    previous_document IS NULL OR jsonb_typeof(previous_document) = 'object'
  ),
  CONSTRAINT audit_events_resulting_chk CHECK (
    resulting_document IS NULL OR jsonb_typeof(resulting_document) = 'object'
  ),
  CONSTRAINT audit_events_context_chk CHECK (
    context_document IS NULL OR jsonb_typeof(context_document) = 'object'
  )
);

COMMENT ON TABLE amlog.audit_events IS
  'Append-only audit history. Application writers are expected to add a row in the same transaction as the change.';

-- ---------------------------------------------------------------------------
-- Evaluation labels, outside the operational scoring tables
-- ---------------------------------------------------------------------------

CREATE TABLE amlog_label.alert_labels (
  alert_label_id UUID NOT NULL DEFAULT gen_random_uuid(),
  alert_id UUID NOT NULL,
  label_value TEXT NOT NULL,
  label_source TEXT NOT NULL,
  data_origin TEXT NOT NULL,
  label_rationale TEXT,
  labeled_at TIMESTAMPTZ NOT NULL,
  labeled_by TEXT NOT NULL,
  eligible_for_evaluation BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT alert_labels_pkey PRIMARY KEY (alert_label_id),
  CONSTRAINT alert_labels_alert_fk
    FOREIGN KEY (alert_id) REFERENCES amlog.alerts (alert_id) ON DELETE RESTRICT,
  CONSTRAINT alert_labels_value_chk CHECK (
    label_value IN (
      'productive_alert',
      'non_productive_alert',
      'uncertain',
      'excluded'
    )
  ),
  CONSTRAINT alert_labels_source_chk CHECK (
    label_source IN (
      'investigator_outcome',
      'quality_review',
      'synthetic_fixture',
      'historical_import'
    )
  ),
  CONSTRAINT alert_labels_origin_chk CHECK (
    data_origin IN ('operational', 'synthetic', 'historical_import')
  ),
  CONSTRAINT alert_labels_synthetic_chk CHECK (
    (
      label_source = 'synthetic_fixture'
      AND data_origin = 'synthetic'
    )
    OR (
      label_source <> 'synthetic_fixture'
      AND data_origin <> 'synthetic'
    )
  ),
  CONSTRAINT alert_labels_rationale_chk CHECK (
    data_origin = 'operational'
    OR length(btrim(coalesce(label_rationale, ''))) > 0
  ),
  CONSTRAINT alert_labels_labeled_by_chk CHECK (length(btrim(labeled_by)) > 0)
);

COMMENT ON TABLE amlog_label.alert_labels IS
  'Append-only evaluation labels. Productive means the alert was useful to review. It does not mean a legal finding was made.';

COMMENT ON COLUMN amlog_label.alert_labels.eligible_for_evaluation IS
  'False until a governed decision includes the label in evaluation. Data origin must still be disclosed.';

-- ---------------------------------------------------------------------------
-- Indexes for expected access paths
-- These are a starting set, not a claim that they have been tuned on a workload.
-- ---------------------------------------------------------------------------

CREATE INDEX transactions_account_event_idx
  ON amlog.transactions (account_id, event_timestamp DESC);

CREATE INDEX transactions_customer_event_idx
  ON amlog.transactions (customer_id, event_timestamp DESC);

CREATE INDEX transactions_channel_event_idx
  ON amlog.transactions (payment_channel, event_timestamp DESC);

CREATE INDEX transactions_type_event_idx
  ON amlog.transactions (transaction_type, event_timestamp DESC);

CREATE INDEX transactions_counterparty_reference_idx
  ON amlog.transactions (counterparty_reference, event_timestamp DESC)
  WHERE counterparty_reference IS NOT NULL;

CREATE INDEX transactions_event_brin_idx
  ON amlog.transactions USING BRIN (event_timestamp);

CREATE INDEX transactions_ingested_idx
  ON amlog.transactions (ingested_at);

CREATE INDEX monitoring_rules_status_effective_idx
  ON amlog.monitoring_rules (lifecycle_status, effective_from);

CREATE INDEX investigation_cases_customer_opened_idx
  ON amlog.investigation_cases (customer_id, opened_at DESC);

CREATE INDEX investigation_cases_status_priority_idx
  ON amlog.investigation_cases (lifecycle_status, priority);

CREATE INDEX investigation_notes_case_noted_idx
  ON amlog.investigation_notes (investigation_case_id, noted_at);

CREATE INDEX alerts_customer_triggered_idx
  ON amlog.alerts (customer_id, triggered_at DESC);

CREATE INDEX alerts_status_triggered_idx
  ON amlog.alerts (lifecycle_status, triggered_at DESC);

CREATE INDEX alerts_rule_idx
  ON amlog.alerts (monitoring_rule_id);

CREATE INDEX alerts_case_idx
  ON amlog.alerts (investigation_case_id)
  WHERE investigation_case_id IS NOT NULL;

CREATE INDEX alerts_account_idx
  ON amlog.alerts (account_id)
  WHERE account_id IS NOT NULL;

CREATE INDEX alert_transactions_transaction_idx
  ON amlog.alert_transactions (transaction_id);

CREATE INDEX feature_computations_alert_computed_idx
  ON amlog.feature_computations (alert_id, computed_at DESC);

CREATE INDEX alert_features_alert_name_idx
  ON amlog.alert_features (alert_id, feature_name);

CREATE INDEX alert_features_computation_idx
  ON amlog.alert_features (feature_computation_id);

CREATE INDEX model_versions_status_idx
  ON amlog.model_versions (lifecycle_status);

CREATE INDEX risk_scores_alert_scored_idx
  ON amlog.risk_scores (alert_id, scored_at DESC);

CREATE INDEX risk_scores_model_scored_idx
  ON amlog.risk_scores (model_version_id, scored_at DESC);

CREATE INDEX risk_scores_run_idx
  ON amlog.risk_scores (scoring_run_id);

CREATE INDEX risk_scores_computation_idx
  ON amlog.risk_scores (feature_computation_id);

CREATE INDEX audit_events_entity_idx
  ON amlog.audit_events (entity_schema, entity_table, entity_id, occurred_at DESC);

CREATE INDEX audit_events_actor_idx
  ON amlog.audit_events (actor_reference, occurred_at DESC);

CREATE INDEX audit_events_correlation_idx
  ON amlog.audit_events (correlation_id)
  WHERE correlation_id IS NOT NULL;

CREATE INDEX audit_events_occurred_brin_idx
  ON amlog.audit_events USING BRIN (occurred_at);

CREATE INDEX alert_labels_alert_labeled_idx
  ON amlog_label.alert_labels (alert_id, labeled_at DESC);

-- ---------------------------------------------------------------------------
-- Integrity functions that read tables
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION amlog.protect_customer()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.customer_id IS DISTINCT FROM NEW.customer_id
       OR OLD.customer_reference IS DISTINCT FROM NEW.customer_reference
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
      RAISE EXCEPTION 'customer identity is immutable'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_source_identity()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.source_identity_id IS DISTINCT FROM NEW.source_identity_id
       OR OLD.customer_id IS DISTINCT FROM NEW.customer_id
       OR OLD.source_system IS DISTINCT FROM NEW.source_system
       OR OLD.identity_type IS DISTINCT FROM NEW.identity_type
       OR OLD.identity_value IS DISTINCT FROM NEW.identity_value
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
      RAISE EXCEPTION 'source identity keys are immutable; close the row and insert a new one'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_account()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.account_id IS DISTINCT FROM NEW.account_id
       OR OLD.customer_id IS DISTINCT FROM NEW.customer_id
       OR OLD.account_reference IS DISTINCT FROM NEW.account_reference
       OR OLD.source_system IS DISTINCT FROM NEW.source_system
       OR OLD.source_account_id IS DISTINCT FROM NEW.source_account_id
       OR OLD.currency_code IS DISTINCT FROM NEW.currency_code
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
      RAISE EXCEPTION 'account identity, owner, and currency are immutable'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.enforce_transaction_account_owner()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
DECLARE
  owner_id UUID;
BEGIN
  SELECT customer_id
    INTO owner_id
    FROM amlog.accounts
   WHERE account_id = NEW.account_id;

  IF owner_id IS NULL THEN
    RAISE EXCEPTION 'transaction account % does not exist', NEW.account_id
      USING ERRCODE = '23503';
  END IF;

  IF owner_id IS DISTINCT FROM NEW.customer_id THEN
    RAISE EXCEPTION 'transaction customer must be the current owner of the account'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_monitoring_rule()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
      USING ERRCODE = '55000';
  END IF;

  IF OLD.monitoring_rule_id IS DISTINCT FROM NEW.monitoring_rule_id
     OR OLD.rule_code IS DISTINCT FROM NEW.rule_code
     OR OLD.version_number IS DISTINCT FROM NEW.version_number
     OR OLD.authored_by IS DISTINCT FROM NEW.authored_by
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
  THEN
    RAISE EXCEPTION 'monitoring rule identity is immutable'
      USING ERRCODE = '55000';
  END IF;

  IF OLD.lifecycle_status = 'draft' THEN
    IF NEW.lifecycle_status NOT IN ('draft', 'active', 'retired') THEN
      RAISE EXCEPTION 'draft monitoring rule cannot move to %', NEW.lifecycle_status
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.rule_name IS DISTINCT FROM NEW.rule_name
     OR OLD.description IS DISTINCT FROM NEW.description
     OR OLD.parameter_document IS DISTINCT FROM NEW.parameter_document
     OR OLD.severity IS DISTINCT FROM NEW.severity
     OR OLD.effective_from IS DISTINCT FROM NEW.effective_from
  THEN
    RAISE EXCEPTION 'a published monitoring rule version is immutable; create a new version'
      USING ERRCODE = '55000';
  END IF;

  IF OLD.lifecycle_status = 'active' THEN
    IF NEW.lifecycle_status NOT IN ('active', 'retired') THEN
      RAISE EXCEPTION 'an active monitoring rule can only stay active or be retired'
        USING ERRCODE = '55000';
    END IF;
    IF OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN
      RAISE EXCEPTION 'effective_to cannot be rewritten once it is set'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'retired' THEN
    IF NEW.lifecycle_status IS DISTINCT FROM OLD.lifecycle_status
       OR NEW.effective_to IS DISTINCT FROM OLD.effective_to
    THEN
      RAISE EXCEPTION 'a retired monitoring rule version cannot be changed'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'monitoring rule status % is not recognized', OLD.lifecycle_status
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_investigation_case()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF OLD.investigation_case_id IS DISTINCT FROM NEW.investigation_case_id
       OR OLD.case_reference IS DISTINCT FROM NEW.case_reference
       OR OLD.customer_id IS DISTINCT FROM NEW.customer_id
       OR OLD.opened_at IS DISTINCT FROM NEW.opened_at
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
      RAISE EXCEPTION 'investigation case identity is immutable'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_alert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
DECLARE
  rule_status TEXT;
  rule_code TEXT;
  rule_version INTEGER;
  account_owner_id UUID;
  case_customer_id UUID;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
      USING ERRCODE = '55000';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    IF OLD.alert_id IS DISTINCT FROM NEW.alert_id
       OR OLD.alert_reference IS DISTINCT FROM NEW.alert_reference
       OR OLD.monitoring_rule_id IS DISTINCT FROM NEW.monitoring_rule_id
       OR OLD.recorded_rule_code IS DISTINCT FROM NEW.recorded_rule_code
       OR OLD.recorded_rule_version IS DISTINCT FROM NEW.recorded_rule_version
       OR OLD.customer_id IS DISTINCT FROM NEW.customer_id
       OR OLD.account_id IS DISTINCT FROM NEW.account_id
       OR OLD.triggered_at IS DISTINCT FROM NEW.triggered_at
       OR OLD.window_started_at IS DISTINCT FROM NEW.window_started_at
       OR OLD.window_ended_at IS DISTINCT FROM NEW.window_ended_at
       OR OLD.summary IS DISTINCT FROM NEW.summary
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
      RAISE EXCEPTION 'alert identity and generation snapshot are immutable'
        USING ERRCODE = '55000';
    END IF;
  END IF;

  IF TG_OP = 'INSERT' THEN
    SELECT lifecycle_status, monitoring_rules.rule_code, version_number
      INTO rule_status, rule_code, rule_version
      FROM amlog.monitoring_rules
     WHERE monitoring_rule_id = NEW.monitoring_rule_id;

    IF rule_status IS NULL THEN
      RAISE EXCEPTION 'monitoring rule % does not exist', NEW.monitoring_rule_id
        USING ERRCODE = '23503';
    END IF;

    IF rule_status <> 'active' THEN
      RAISE EXCEPTION 'alerts must cite an active monitoring rule version'
        USING ERRCODE = '23514';
    END IF;

    IF NEW.recorded_rule_code IS DISTINCT FROM rule_code
       OR NEW.recorded_rule_version IS DISTINCT FROM rule_version
    THEN
      RAISE EXCEPTION 'alert rule snapshot must match the monitoring rule version'
        USING ERRCODE = '23514';
    END IF;

    IF NEW.account_id IS NOT NULL THEN
      SELECT customer_id
        INTO account_owner_id
        FROM amlog.accounts
       WHERE account_id = NEW.account_id;

      IF account_owner_id IS NULL THEN
        RAISE EXCEPTION 'alert account % does not exist', NEW.account_id
          USING ERRCODE = '23503';
      END IF;

      IF account_owner_id IS DISTINCT FROM NEW.customer_id THEN
        RAISE EXCEPTION 'alert account must belong to the alert customer'
          USING ERRCODE = '23514';
      END IF;
    END IF;
  END IF;

  IF NEW.investigation_case_id IS NOT NULL THEN
    SELECT customer_id
      INTO case_customer_id
      FROM amlog.investigation_cases
     WHERE investigation_case_id = NEW.investigation_case_id;

    IF case_customer_id IS NULL THEN
      RAISE EXCEPTION 'investigation case % does not exist', NEW.investigation_case_id
        USING ERRCODE = '23503';
    END IF;

    IF case_customer_id IS DISTINCT FROM NEW.customer_id THEN
      RAISE EXCEPTION 'alert customer and investigation case customer must match'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION amlog.protect_model_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'operation % is not allowed on %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
      USING ERRCODE = '55000';
  END IF;

  IF OLD.model_version_id IS DISTINCT FROM NEW.model_version_id
     OR OLD.model_name IS DISTINCT FROM NEW.model_name
     OR OLD.version_label IS DISTINCT FROM NEW.version_label
     OR OLD.algorithm_family IS DISTINCT FROM NEW.algorithm_family
     OR OLD.feature_set_name IS DISTINCT FROM NEW.feature_set_name
     OR OLD.feature_set_version IS DISTINCT FROM NEW.feature_set_version
     OR OLD.training_data_provenance IS DISTINCT FROM NEW.training_data_provenance
     OR OLD.registered_at IS DISTINCT FROM NEW.registered_at
     OR OLD.registered_by IS DISTINCT FROM NEW.registered_by
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
  THEN
    RAISE EXCEPTION 'model version identity and provenance are immutable; register a new version'
      USING ERRCODE = '55000';
  END IF;

  IF OLD.lifecycle_status <> 'registered' THEN
    IF OLD.artifact_reference IS DISTINCT FROM NEW.artifact_reference
       OR OLD.evaluation_summary IS DISTINCT FROM NEW.evaluation_summary
    THEN
      RAISE EXCEPTION 'artifact and evaluation details are frozen once a model leaves registered'
        USING ERRCODE = '55000';
    END IF;
  END IF;

  IF OLD.lifecycle_status = NEW.lifecycle_status
     AND OLD.approved_at IS NOT DISTINCT FROM NEW.approved_at
     AND OLD.approved_by IS NOT DISTINCT FROM NEW.approved_by
  THEN
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'registered'
     AND NEW.lifecycle_status IN ('validated', 'rejected')
  THEN
    IF NEW.approved_at IS NOT NULL OR NEW.approved_by IS NOT NULL THEN
      RAISE EXCEPTION 'approval fields are set only when a model version is approved'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'validated' AND NEW.lifecycle_status = 'rejected' THEN
    IF NEW.approved_at IS NOT NULL OR NEW.approved_by IS NOT NULL THEN
      RAISE EXCEPTION 'approval fields are set only when a model version is approved'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'validated' AND NEW.lifecycle_status = 'approved' THEN
    IF NEW.approved_at IS NULL OR NEW.approved_by IS NULL THEN
      RAISE EXCEPTION 'an approved model version requires approved_at and approved_by'
        USING ERRCODE = '23514';
    END IF;
    IF OLD.approved_at IS NOT NULL OR OLD.approved_by IS NOT NULL THEN
      RAISE EXCEPTION 'approval fields cannot be rewritten'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'approved' AND NEW.lifecycle_status = 'retired' THEN
    IF NEW.approved_at IS DISTINCT FROM OLD.approved_at
       OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
    THEN
      RAISE EXCEPTION 'approval fields cannot be rewritten'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'model version status cannot change from % to %',
    OLD.lifecycle_status, NEW.lifecycle_status
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION amlog.enforce_score_snapshot()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
DECLARE
  computation_alert_id UUID;
  computation_feature_set_name TEXT;
  computation_feature_set_version TEXT;
  computation_fingerprint TEXT;
  model_name TEXT;
  version_label TEXT;
  algorithm_family TEXT;
  model_feature_set_name TEXT;
  model_feature_set_version TEXT;
  model_status TEXT;
BEGIN
  SELECT alert_id, feature_set_name, feature_set_version, input_fingerprint
    INTO computation_alert_id,
         computation_feature_set_name,
         computation_feature_set_version,
         computation_fingerprint
    FROM amlog.feature_computations
   WHERE feature_computation_id = NEW.feature_computation_id;

  IF computation_alert_id IS NULL THEN
    RAISE EXCEPTION 'feature computation % does not exist', NEW.feature_computation_id
      USING ERRCODE = '23503';
  END IF;

  IF computation_alert_id IS DISTINCT FROM NEW.alert_id
     OR computation_feature_set_name IS DISTINCT FROM NEW.feature_set_name
     OR computation_feature_set_version IS DISTINCT FROM NEW.feature_set_version
     OR computation_fingerprint IS DISTINCT FROM NEW.recorded_input_fingerprint
  THEN
    RAISE EXCEPTION 'risk score inputs must match the feature computation snapshot'
      USING ERRCODE = '23514';
  END IF;

  SELECT model_versions.model_name,
         model_versions.version_label,
         model_versions.algorithm_family,
         model_versions.feature_set_name,
         model_versions.feature_set_version,
         lifecycle_status
    INTO model_name,
         version_label,
         algorithm_family,
         model_feature_set_name,
         model_feature_set_version,
         model_status
    FROM amlog.model_versions
   WHERE model_version_id = NEW.model_version_id;

  IF model_status IS NULL THEN
    RAISE EXCEPTION 'model version % does not exist', NEW.model_version_id
      USING ERRCODE = '23503';
  END IF;

  IF model_name IS DISTINCT FROM NEW.recorded_model_name
     OR version_label IS DISTINCT FROM NEW.recorded_version_label
     OR algorithm_family IS DISTINCT FROM NEW.recorded_algorithm_family
  THEN
    RAISE EXCEPTION 'risk score model snapshot must match the model version'
      USING ERRCODE = '23514';
  END IF;

  -- The model version names the only feature set it may score.
  -- The snapshot and the values stored on the score must use that exact set.
  IF model_feature_set_name IS DISTINCT FROM computation_feature_set_name
     OR model_feature_set_version IS DISTINCT FROM computation_feature_set_version
     OR model_feature_set_name IS DISTINCT FROM NEW.feature_set_name
     OR model_feature_set_version IS DISTINCT FROM NEW.feature_set_version
  THEN
    RAISE EXCEPTION 'risk score feature set must match the feature set required by the model version'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.score_use = 'operational' AND model_status <> 'approved' THEN
    RAISE EXCEPTION 'operational scores require an approved model version'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.score_use = 'experiment' AND model_status NOT IN ('validated', 'approved') THEN
    RAISE EXCEPTION 'experiment scores require a validated or approved model version'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION amlog.record_audit_event(
  p_actor_type TEXT,
  p_actor_reference TEXT,
  p_action TEXT,
  p_entity_schema TEXT,
  p_entity_table TEXT,
  p_entity_id UUID,
  p_correlation_id UUID,
  p_request_reference TEXT,
  p_previous_document JSONB,
  p_resulting_document JSONB,
  p_context_document JSONB
)
RETURNS UUID
LANGUAGE plpgsql
SET search_path = amlog, pg_temp
AS $$
DECLARE
  new_id UUID;
BEGIN
  INSERT INTO amlog.audit_events (
    actor_type,
    actor_reference,
    action,
    entity_schema,
    entity_table,
    entity_id,
    correlation_id,
    request_reference,
    previous_document,
    resulting_document,
    context_document
  )
  VALUES (
    p_actor_type,
    p_actor_reference,
    p_action,
    p_entity_schema,
    p_entity_table,
    p_entity_id,
    p_correlation_id,
    p_request_reference,
    p_previous_document,
    p_resulting_document,
    p_context_document
  )
  RETURNING audit_event_id INTO new_id;

  RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION amlog.protect_customer() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_source_identity() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_account() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.enforce_transaction_account_owner() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_monitoring_rule() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_investigation_case() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_alert() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.protect_model_version() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.enforce_score_snapshot() FROM PUBLIC;
REVOKE ALL ON FUNCTION amlog.record_audit_event(
  TEXT, TEXT, TEXT, TEXT, TEXT, UUID, UUID, TEXT, JSONB, JSONB, JSONB
) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

CREATE TRIGGER schema_migrations_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.schema_migrations
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER schema_migrations_reject_truncate
BEFORE TRUNCATE ON amlog.schema_migrations
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER customers_touch_updated_at
BEFORE UPDATE ON amlog.customers
FOR EACH ROW
EXECUTE FUNCTION amlog.touch_updated_at();

CREATE TRIGGER customers_protect
BEFORE UPDATE OR DELETE ON amlog.customers
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_customer();

CREATE TRIGGER customers_reject_truncate
BEFORE TRUNCATE ON amlog.customers
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER source_identities_touch_updated_at
BEFORE UPDATE ON amlog.source_identities
FOR EACH ROW
EXECUTE FUNCTION amlog.touch_updated_at();

CREATE TRIGGER source_identities_protect
BEFORE UPDATE OR DELETE ON amlog.source_identities
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_source_identity();

CREATE TRIGGER source_identities_reject_truncate
BEFORE TRUNCATE ON amlog.source_identities
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER accounts_touch_updated_at
BEFORE UPDATE ON amlog.accounts
FOR EACH ROW
EXECUTE FUNCTION amlog.touch_updated_at();

CREATE TRIGGER accounts_protect
BEFORE UPDATE OR DELETE ON amlog.accounts
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_account();

CREATE TRIGGER accounts_reject_truncate
BEFORE TRUNCATE ON amlog.accounts
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER transactions_enforce_owner
BEFORE INSERT ON amlog.transactions
FOR EACH ROW
EXECUTE FUNCTION amlog.enforce_transaction_account_owner();

CREATE TRIGGER transactions_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.transactions
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER transactions_reject_truncate
BEFORE TRUNCATE ON amlog.transactions
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER monitoring_rules_protect
BEFORE UPDATE OR DELETE ON amlog.monitoring_rules
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_monitoring_rule();

CREATE TRIGGER monitoring_rules_reject_truncate
BEFORE TRUNCATE ON amlog.monitoring_rules
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER investigation_cases_touch_updated_at
BEFORE UPDATE ON amlog.investigation_cases
FOR EACH ROW
EXECUTE FUNCTION amlog.touch_updated_at();

CREATE TRIGGER investigation_cases_protect
BEFORE UPDATE OR DELETE ON amlog.investigation_cases
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_investigation_case();

CREATE TRIGGER investigation_cases_reject_truncate
BEFORE TRUNCATE ON amlog.investigation_cases
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER investigation_notes_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.investigation_notes
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER investigation_notes_reject_truncate
BEFORE TRUNCATE ON amlog.investigation_notes
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alerts_touch_updated_at
BEFORE UPDATE ON amlog.alerts
FOR EACH ROW
EXECUTE FUNCTION amlog.touch_updated_at();

CREATE TRIGGER alerts_protect
BEFORE INSERT OR UPDATE OR DELETE ON amlog.alerts
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_alert();

CREATE TRIGGER alerts_reject_truncate
BEFORE TRUNCATE ON amlog.alerts
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_transactions_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.alert_transactions
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_transactions_reject_truncate
BEFORE TRUNCATE ON amlog.alert_transactions
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER feature_computations_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.feature_computations
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER feature_computations_reject_truncate
BEFORE TRUNCATE ON amlog.feature_computations
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_features_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.alert_features
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_features_reject_truncate
BEFORE TRUNCATE ON amlog.alert_features
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER model_versions_protect
BEFORE UPDATE OR DELETE ON amlog.model_versions
FOR EACH ROW
EXECUTE FUNCTION amlog.protect_model_version();

CREATE TRIGGER model_versions_reject_truncate
BEFORE TRUNCATE ON amlog.model_versions
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER risk_scores_enforce_snapshot
BEFORE INSERT ON amlog.risk_scores
FOR EACH ROW
EXECUTE FUNCTION amlog.enforce_score_snapshot();

CREATE TRIGGER risk_scores_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.risk_scores
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER risk_scores_reject_truncate
BEFORE TRUNCATE ON amlog.risk_scores
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER audit_events_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog.audit_events
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER audit_events_reject_truncate
BEFORE TRUNCATE ON amlog.audit_events
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_labels_reject_row_mutation
BEFORE UPDATE OR DELETE ON amlog_label.alert_labels
FOR EACH ROW
EXECUTE FUNCTION amlog.reject_mutation();

CREATE TRIGGER alert_labels_reject_truncate
BEFORE TRUNCATE ON amlog_label.alert_labels
FOR EACH STATEMENT
EXECUTE FUNCTION amlog.reject_mutation();

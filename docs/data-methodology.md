# Data methodology

## What this repository contains

This repository contains design documents, a PostgreSQL schema, a migration runner, a transaction-ingestion function, a fictional workload generator, two versioned monitoring rules, a local investigator review page, a provisional anomaly ranking, and tests. It contains no customer file, no transaction file, no operational population, and no approved production model.

A local PostgreSQL 16 database can be started with Docker Compose. Applying the migration creates empty tables and one schema-version row. It does not load rules, models, or scores. No external source system is connected. This database is a development instance on the workstation. It is not a client system and it is not an AWS deployment.

One development fixture can be committed by running `python -m amlog.ingest.demo`. It inserts a single customer, account, and payment so a person can inspect one row actually stored in PostgreSQL. The source system is `amlog-development`. The customer reference is `AMLOG-DEV-CUSTOMER-001`, the account reference is `AMLOG-DEV-ACCOUNT-001`, and the source event identifier is `AMLOG-DEV-PAYMENT-001`. The legal name is `AMLOG development fixture`. These values are reserved for that local demonstration. They are not operational history, not a customer population, and not training data. Automated tests use other identifiers and roll their rows back, so they do not add to this fixture.

scikit-learn is a dependency of the provisional anomaly ranking. Its presence is not evidence of an approved production model, a client deployment, or measured detection quality. The workload generator uses only the Python standard library and the project's existing database driver.

## Fictional workload

The command `python -m amlog.workload.load` can create a disposable PostgreSQL database, default name `amlog_workload`, and load fictional customers, accounts, and payments into it. It refuses the development database. `--reset` drops only that disposable database.

The parties are invented from fixed name lists and a numeric seed. They are not a sample of real customers. Payment events are schema 2 events and pass through the ingestion function, so duplicate delivery is still rejected or acknowledged and each new payment still gets an audit row. The customer on each payment is the customer stored on the account.

Scenario codes describe the shape of the invented activity. They include ordinary spending, salary, recurring payments, merchant sales, business payments, cross-border wires, seasonal spending, high-frequency purchases, a marketplace receipt followed by a payment from a different account, a larger one-off amount, and a first payment to a new counterparty. They also include same-account receipt and outgoing pairs: below, exactly at, and above an 80 percent ratio, a pair outside 24 hours, a smaller supplier payment, a smaller recurring expense, and an outgoing payment in another currency. `same_account_split` follows one receipt with several outgoing payments that individually stay under 80 percent and together reach it. `same_account_partial` stays under that ratio. `velocity_burst` places six to eight card payments inside one hour. `velocity_short` stops at five, and `velocity_wide` spreads six payments beyond one hour. An episode is started only when every payment in it fits in the party's remaining events and at or before the history end. A payment whose own time would fall behind the previous payment on that party is placed two hours later when the history still has room, so unrelated payments are not compressed into a one-second stack. Those codes are stored in `amlog_scenario.event_annotations` with a fixed note that they are development scenarios. They are not written on `amlog.transactions`. They are not fraud judgments, alert labels, or training labels. Meeting a monitoring threshold in this workload is not a finding that the activity is suspicious.

The same seed and population id reproduce the same events. A different seed or population id builds an independent set. The event count has no fixed ceiling. The default command uses 2,000 events so a local check stays modest. A larger count, including 100,000 or more, runs only when requested. A timing from one workstation is a measurement of that run. It is not a capacity claim and it is not a detection-quality result.

`python -m amlog.monitor.run --database amlog_workload` replays R001 over that stored history. `python -m amlog.monitor.run --rule R002 --database amlog_workload` replays R002. R002 compares an incoming amount with later outgoing amounts in the same currency. It does not convert currencies, and it does not treat the result as proof that the same funds moved. Neither replay reads `amlog_scenario` or `amlog_label`. Alerts written by those commands are historical replay results for fictional activity. They are not live monitoring alerts, not fraud determinations, and not a false-positive measurement. The development database is refused, so the demonstration payment is not alerted. Replaying R002 does not rewrite R001 alerts.

One integrated population uses seed `20261026` and population id `integrated`. It was loaded into the disposable database `amlog_workload_integrated` with 10,000 payments. R001 and R002 were each replayed twice on that history. Counts from that run describe the fictional population only.

## Alert features

`python -m amlog.features.run` writes feature set `alert_behavior` version `1` into `amlog.feature_computations` and `amlog.alert_features`. One snapshot describes one alert. It is not a score, and an alert is not treated as a training label.

The evaluation clock is `alerts.triggered_at`, which the monitoring replay sets to the triggering payment's event time. A payment is visible when its `event_timestamp` is at or before that instant and inside the 30-day lookback, and when it belongs to the alert's account. The closed windows are one hour, 24 hours, seven days, and 30 days. `booked_at` and `ingested_at` are not used. On a batch-loaded history, `ingested_at` is the time the row was stored, which can be long after the event. This snapshot is therefore a reconstruction along event time. It will include a late-arriving payment whose event time is already in the past. An operational decision path would also need to know whether that payment had been stored before the decision. That second cut is not applied here.

`history_covers_1h`, `history_covers_24h`, `history_covers_7d`, and `history_covers_30d` are true only when `accounts.opened_at` is known and the account was already open for the whole window. Counts still report the payments that were visible. A count of zero next to a false history flag means the window was not fully observed. It does not mean the account was inactive for that whole window. `new_counterparty_count_24h` and the two rate features are omitted unless the account was open for the baseline they need. A rate is also omitted when the baseline count is zero, so a quiet baseline is not stored as an infinite ratio.

Amount sums, the maximum, and the mean use the account currency only. A payment in another currency increments `other_currency_count_*` and `distinct_currency_count_30d`. It is not converted. `base_amount` and `fx_rate` are not read. An amount statistic is omitted when no same-currency payment of that direction exists in the window, because the schema cannot store a null numeric value and a stored zero would look like a real total.

The input fingerprint is a hash of the feature-set identity, the alert, the account opening time, and the visible payments. The same fingerprint is not stored twice. A different fingerprint is a new snapshot. The earlier snapshot remains. `computed_at` is when the snapshot was written, not the evaluation time. The evaluation time is the feature `evaluation_event_time`. `availability_basis` is the text `event_timestamp`.

The snapshot reads `amlog.alerts`, `amlog.accounts`, `amlog.alert_transactions`, and `amlog.transactions`. It does not read `amlog_scenario`, `amlog_label`, investigation dispositions, or case outcomes. `rule_code` and `rule_version` are copied from the alert so a later model can tell which rule asked for the review. They are not a judgment that the activity is suspicious.

Illustrative currency rates in the generator are fixed factors used to fill optional base-amount fields. They are not a market rate feed.

## Investigator review

`python -m amlog.review.serve` opens a page on `127.0.0.1`. The default database is `amlog_workload_integrated`. The development database is refused. The page does not read `amlog_scenario`.

A reviewer can open one case, record a finding, record whether the alert was useful to review, close the case, and reopen it. Usefulness is stored as a separate label: `productive_alert`, `non_productive_alert`, or `uncertain`. The stored source is `investigator_outcome` and the stored origin is `operational`. A judgment entered in this review is a current review record, so it is not given the historical-import origin. `eligible_for_evaluation` is false. The rationale says the row is not a finding that a crime occurred or that none occurred, and not a training label. The stored payments may still be development history. An existing label row is not rewritten.

The operational close uses `no_further_action`, `continue_monitoring`, `escalated`, or `inconclusive`. That disposition is not copied into the label. The word inconclusive on the case is the operational outcome. The word inconclusive on the usefulness question is stored as `uncertain`.

Notes and labels are append-only. A changed assessment is a new label, and the earlier label remains. An identical current assessment is rejected. Closing and reopening write an audit row that keeps the previous disposition. The reviewer reference, the time, and the alert identifier are stored with the action. No alert receives an assessment unless a person saves one. A rule code is not turned into a label.

A usable training outcome would be a label with `eligible_for_evaluation` true, a source of `investigator_outcome` or `quality_review`, and a value of `productive_alert`, `non_productive_alert`, or `uncertain`. This page never sets that flag. On the integrated workload, before any person saves a review, the usable training count is zero. Promoting an assessment would require a later governed insert. What is still missing for credible supervised training is that promotion, reviews by more than one person, enough productive and non-productive outcomes, disclosure that this population is fictional, and a separate evaluation split. Scenario codes, rule codes, model output, and the absence of a case are not labels.

## Provisional anomaly ranking

`python -m amlog.prioritize.run` reads the latest `alert_behavior` version 1 snapshot for each alert. It does not read `amlog_label`, `amlog_scenario`, or investigation dispositions. Rule code, rule version, record identifiers, and the evaluation clock are not model columns.

The fit uses an account-level hash split. Every alert for one account is in the fit or in the holdout, not both. Median imputation and within-currency amount scaling are estimated on the training accounts only. A raw amount is not placed next to an amount in another currency. A currency with fewer than eight training observations, or with no spread, stays missing and is imputed only after that decision. A missing feature also keeps a missingness indicator. A constant training column is dropped.

The stored score is the negated Isolation Forest decision function, so a higher number means more unusual. A second score is the average absolute robust z-score on the same matrix. That second score is the non-ML comparison. Their holdout rank agreement describes how similarly the two rankings order unseen accounts. It is not precision, recall, or ROC-AUC, and it is not a measure of crime detection. Neither score is a probability of financial crime, a productive investigation, or a false positive.

The review page lists the features farthest from the typical training alert. Each figure is a robust z-score, which is a distance. It does not say that the feature caused the rank, caused the alert, or shows a crime or a false positive.

The fitted Isolation Forest and the training-only preprocessing parameters are written under `var/model-artifacts`, which is local output and is not published. The database `artifact_reference` remains the digest of those parameters. A later run loads the file and scores the same snapshots without fitting again. It does not insert another score and it does not edit a validated model row. If the file is missing, one refit is kept only when its parameter digest and its scores match the rows already stored.

Both model versions are `validated` and not `approved`. The scores are `experiment` scores. The database rejects an operational score from these versions. Training-account scores are in-sample. The population is generated, and repeated account behavior and designed scenarios are correlated, so a high rank can rediscover a generator shape.

`python -m amlog.prioritize.supervised_run` is the supervised entry point. It counts the latest eligible `productive_alert` and `non_productive_alert` labels from `investigator_outcome` or `quality_review`. The floor is 25 alerts in each class and two distinct reviewers. Below that floor the command prints `BLOCKED` and does not fit a classifier. It does not manufacture labels and it does not report precision, recall, ROC-AUC, or a business saving. A synthetic fixture label does not count, and a label with `eligible_for_evaluation` false does not count.

## What the tests actually do

Two kinds of checks are part of the foundation:

- Schema contract tests read `db/migrations/0001_initial_schema.sql` and `docs/database-design.md`. They check tables, exact numeric types, time-zone-aware timestamps, keys, the label-schema boundary, version immutability, and the absence of seed data.
- Runner tests execute the migration program against an in-memory stand-in for a database connection. They check version order, checksum refusal, and rollback behavior.
- PostgreSQL integration tests run only when `AMLOG_DATABASE_URL` points at a database and `psycopg` is installed. They check foreign keys, duplicate source events, transaction immutability, model approval, feature-set compatibility, and transaction ingestion. Ingestion tests cover a valid payment, an invalid amount, an unknown account, an unknown source account, wallet, bank-transfer, and merchant-payment sources, agreement between the two account identifiers, an identical replay, a conflicting replay, timestamps, account-owner resolution, and rollback. A separate workload test loads a small fictional population into database `amlog_workload_test`, checks ownership, time order, scenario coverage, and duplicate replay, then drops that database. Monitoring tests use database `amlog_monitor_test` or `amlog_r002_test` and drop it as well. Feature tests use `amlog_feature_test` and drop it as well. They cover the event-time cutoff, the closed window edges, a second account, a second currency, a short account history, a repeated snapshot, and a new snapshot after the inputs change. Review tests use `amlog_review_test` and drop it as well. They cover a saved review, a repeated open, a rejected duplicate assessment, close and reopen, the previous disposition in the audit row, account isolation, a customer mismatch rejected by the database, and the local page. Priority tests use `amlog_priority_test` and drop it as well. They cover training-only imputation, within-currency scaling, an account kept on one side of the split, repeatable ranks, reloading a saved artifact without fitting again, experiment scores, rejection of an operational score and of a score from a model that is only registered, a second run that does not duplicate scores, the review page, and the supervised gate. Monitoring tests cover the five-versus-six boundary, the sixty-minute edge, the R002 amount and 24-hour edges, same-currency allocation, credits, event-time order, a repeated replay, and the links from an alert to its transactions. Each ingestion test rolls back its own transaction. The workload test does not write to the development database. These rows are temporary or disposable fixtures, not a payment dataset, and they are not training data. The committed development payment remains the single schema 1 demonstration row in the development database.

The integration tests do not measure detection quality.

## Provenance rules

Provenance means a reviewer can tell where a data item or a model came from.

| Item | Rule |
| --- | --- |
| Transaction | `source_system` and `source_event_id` identify the upstream event. They are required and unique together. A development demonstration uses source system `amlog-development`. A fictional workload uses population-scoped event ids and must not be described as an operational payment. |
| Rule version | `authored_by` and `created_at` are required. The parameter document is stored with that version and is not executed by this foundation. |
| Feature snapshot | `feature_set_name`, `feature_set_version`, `computed_at`, and `input_fingerprint` identify the snapshot. |
| Model version | `training_data_provenance` is mandatory. It must say whether the data was operational, synthetic, historical import, or a mixture, and must identify the dataset enough for a reviewer to find it. "Internal data" is not an acceptable future value. |
| Score | The score copies the model version and the feature fingerprint. Its feature-set name and version must equal both the snapshot and the feature set required by the model version. `score_use` says `operational` or `experiment`. |
| Label | `label_source` and `data_origin` are mandatory. Synthetic and historical-import labels also require a rationale. `eligible_for_evaluation` defaults to false. |
| Audit event | Actor type, actor reference, action, and the affected table are required. |

A future evaluation report must list the data origin of every label it used. A synthetic fixture must not be described as operational history. An operational score must not be described as validated by a synthetic label.

## Labels are not scoring inputs

Ground truth for training or evaluation is stored only in `amlog_label.alert_labels`.

`amlog.risk_scores` has no label column and no foreign key to the label schema. A case disposition is an operational workflow value on `amlog.investigation_cases`. Copying a disposition into a label, if that is ever done, is a separate governed write. Scoring must not read the label schema, and it must not treat `disposition` as a feature unless a later reviewed design says so explicitly. This foundation does not create features from labels or dispositions.

Label values:

| Value | Meaning |
| --- | --- |
| `productive_alert` | The alert was useful for a person to review. |
| `non_productive_alert` | The alert was not useful for a person to review. |
| `uncertain` | The reviewer could not make that judgment. |
| `excluded` | The row must not be used in an evaluation. |

These words are evaluation labels. They do not record a legal finding, a crime, or a regulatory filing.

The authoritative label for an alert is not overwritten in place. Labels are append-only. Until a later rule is reviewed, consumers should take the latest label by `labeled_at` for a stated `label_source`, and they must still disclose `data_origin`.

## What this methodology does not support

- a claimed false-positive rate, precision, recall, ROC-AUC, or alert-volume reduction
- a claimed investigation-time saving
- a statement that the anomaly rank is a probability of financial crime, a productive investigation, or a false positive
- a statement that a feature deviation caused the rank or the alert
- a statement that the experiment model has been approved for production
- a statement that a real payment source has been connected
- a statement of regulatory certification, regulatory model validation, or production readiness

The local PostgreSQL schema has been applied and the ingestion function has been exercised against it. R001 and R002 have been replayed against a fictional workload. That does not make the development fixture operational data, and it does not support the claims listed above.

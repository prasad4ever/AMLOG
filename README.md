# AMLOG

AMLOG is a client-neutral foundation for prioritizing anti-money laundering alerts and governing the models that support that work. It ingests payment events, stores them in PostgreSQL, replays two versioned monitoring rules, builds a point-in-time feature snapshot for each alert, ranks those alerts with an experimental anomaly model, and lets a person review the same alert on a local page.

The operational ranking is not approved for production. It is not a probability of financial crime, a productive investigation, or a false positive. No operational supervised model has been trained on investigator labels. A separate research benchmark compares declared rankers on the public SynthAML extract and reports precision and recall only for that synthetic benchmark. The logistic regression remains the baseline. A comparison locked on 2020 quarters selected a random forest. No cloud resources have been created. This repository does not claim a client deployment, measured operational performance, or regulatory certification.

## Data Confidentiality & Project Disclaimer

AMLOG is an independent, client-neutral reference implementation demonstrating enterprise Anti-Money Laundering (AML) detection, alert prioritization, and AI/ML model governance capabilities.

In accordance with client confidentiality principles, organizational data protection policies, and proprietary information safeguards, no real client transaction data, confidential business records, or proprietary organizational workflows have been used in this implementation.

The project uses synthetic datasets, including independently generated demonstration workloads and publicly available synthetic research datasets. Customer identities, transaction patterns, business scenarios, and operational workflows are illustrative and do not represent any specific client's actual systems or business operations.

This repository is intended exclusively for architectural demonstration, technical experimentation, and research evaluation. It does not represent a production deployment or an implementation for any identified financial institution. Reported results reflect the documented experimental conditions and should not be interpreted as production performance.

## Architecture

```text
payment event
  -> idempotent ingestion
  -> stored transaction
  -> versioned rule replay (R001, R002)
  -> alert
  -> alert_behavior feature snapshot
  -> experiment anomaly rank and non-ML baseline
  -> local investigator review
```

| Piece | What it does now |
| --- | --- |
| Ingestion | Accepts a versioned `payment_event_received` event, resolves the account, and stores one transaction. An identical delivery is acknowledged. A changed delivery is rejected. |
| Monitoring | Replays R001, outgoing velocity, and R002, rapid funds turnover. Each rule's threshold is stored on its own version. |
| Features | Writes feature set `alert_behavior` version 1. A snapshot uses only that alert's account and payments at or before the alert time. |
| Ranking | Fits an Isolation Forest and a robust z-score baseline. Both are experiment scores. The fitted trees and preprocessing parameters can be saved and loaded again without another fit. |
| Governance | Model versions, feature snapshots, and scores use the existing PostgreSQL tables. Experiment scores require a validated model. Operational scores require an approved model. These rankings are validated for experiment scores and are not approved. |
| Review | One local page shows the alert, its payments, its features, the experiment rank, and the human investigation. |
| Benchmark | A disposable database runs the existing ingestion, R001, and R002 replay, then stores protocol judgments beside those alerts. The judgments are not operational training labels. |
| SynthAML research | An isolated adapter ranks the published SynthAML alerts. The logistic regression is the baseline. A 2020 quarter comparison selected an unapproved random forest. Report and Dismiss stay out of the features and out of the operational training gate. The metrics are research-benchmark results. |

Labels, scenario notes, investigation outcomes, rule codes, and record identifiers are not ranking inputs. Amounts are scaled inside one currency. A currency with too little training history is left missing rather than compared with another currency.

The design documents are `docs/business-requirements.md`, `docs/solution-architecture.md`, `docs/database-design.md`, and `docs/data-methodology.md`.

## Local setup

Python 3.11 or newer is required. The local database is PostgreSQL 16, started with Docker Compose and bound to `127.0.0.1` only.

```powershell
copy .env.example .env
# Edit .env and set POSTGRES_PASSWORD and AMLOG_DATABASE_URL.
docker compose up -d --wait
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[database]"
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_.Trim() -eq '') { return }
  $pair = $_.Split('=', 2)
  Set-Item -Path "Env:$($pair[0])" -Value $pair[1]
}
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.db.migrate
```

The application reads settings from the environment. Docker Compose reads `.env`. `.env.example` has an empty password. The real password belongs only in `.env`, which is gitignored. If `AMLOG_DATABASE_URL` is missing, a database command stops and changes nothing.

`docker compose up` publishes PostgreSQL on `127.0.0.1:5432` and keeps the data in a Docker volume. `docker compose down -v` deletes that volume. Applied SQL files are checksummed. Do not edit `db/migrations/0001_initial_schema.sql` after it has been applied.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AMLOG_ENVIRONMENT` | `local` | `local`, `test`, or `development`. |
| `AMLOG_DATABASE_URL` | empty | PostgreSQL connection string. Empty means do not connect. |
| `AMLOG_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `AMLOG_MIGRATIONS_DIR` | `db/migrations` | Folder of numbered SQL files. |

## Tests

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Schema and runner tests do not need a database. PostgreSQL tests run with the same command when `AMLOG_DATABASE_URL` is set and `psycopg` is installed. They use temporary or disposable databases and do not measure detection quality.

## Demonstration workflow

These commands are the local path from an empty database to a review page. They generate a fictional 2,000-payment workload in `amlog_workload`. They do not load a customer or payment extract. Workload, rule, feature, and ranking commands refuse the development database named by `AMLOG_DATABASE_URL`. `--reset` drops only the disposable database named by `--database`.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.ingest.demo
.\.venv\Scripts\python.exe -m amlog.workload.load --events 2000 --reset
.\.venv\Scripts\python.exe -m amlog.monitor.run --database amlog_workload
.\.venv\Scripts\python.exe -m amlog.monitor.run --rule R002 --database amlog_workload
.\.venv\Scripts\python.exe -m amlog.features.run --database amlog_workload
.\.venv\Scripts\python.exe -m amlog.prioritize.run --database amlog_workload
.\.venv\Scripts\python.exe -m amlog.prioritize.supervised_run --database amlog_workload
.\.venv\Scripts\python.exe -m amlog.review.serve --database amlog_workload --port 8765
```

A separate local run used 10,000 generated payments in `amlog_workload_integrated`, seed `20261026`, and population id `integrated`. A clone does not include that database, its scores, or the fitted artifact files.

The review page is `http://127.0.0.1:8765/`. Sort with `?sort=priority` to see the anomaly order. Entering a reviewer reference is not authentication. The page does not train a model.

## Measured results on the integrated workload

These figures describe one fictional population in the local database `amlog_workload_integrated`. They are not a detection-quality result and they are not a business saving.

| Item | Measured value |
| --- | --- |
| Payments | 10,000 |
| Alerts | 764 (R001: 108, R002: 656) |
| Feature snapshots | 764, with 36,766 feature rows |
| Ranking sample | 601 alerts used to fit the model and 163 alerts from accounts withheld from that fit |
| Model | `alert_priority_isolation_forest` version 1, Isolation Forest, 200 trees, scikit-learn 1.7.2 |
| Comparison | `alert_priority_baseline` version 1, mean absolute robust z-score |
| Holdout rank agreement | 0.505814 between the two rankings. This is not precision, recall, or ROC-AUC |
| Scores stored | 1,528, all `experiment` |
| Governance | Both versions are `validated` for experiment scores. `approved_by` is empty. Operational scoring is rejected |
| Columns kept | 37 behavioral and within-currency amount columns |
| Human reviews | 0 cases, 0 assessments, 0 labels eligible for training |

The kept columns are payment, debit, and credit counts, same-currency and other-currency counts, distinct counterparties, channels, and currencies, channel counts, account age, alert-window length, time since the previous payment, new counterparties, and amount features scaled inside the account currency. Constant training columns were dropped, including the history-coverage flags. A raw amount in one currency is not compared with a raw amount in another.

A repeated ranking command does not insert a second score. If the local artifact file is missing, it is written only when a refit matches the stored parameter digest and the stored scores. Loading that file scores the same snapshots without fitting again.

## Supervised false-positive validation

This is not done.

`python -m amlog.prioritize.supervised_run` checks for eligible reviewed outcomes before it will fit a classifier. An outcome counts only when `eligible_for_evaluation` is true, the source is `investigator_outcome` or `quality_review`, and the latest value is `productive_alert` or `non_productive_alert`. The floor is 25 alerts in each class and two reviewers. The integrated workload has none of those outcomes, so the command stops with `BLOCKED`.

What can be inspected today is the anomaly ordering, its agreement with the non-ML baseline, and the governance state of the experiment scores.

What cannot be measured today is false-positive reduction, precision, recall, ROC-AUC, or any saving in investigator time. No such figure is reported because no eligible outcomes exist. The review page can record a usefulness assessment, and that assessment remains ineligible for training until a later governed decision says otherwise.

`python -m amlog.evaluate.run` checks whether a controlled comparison of a rules-only queue, the anomaly rank, and a supervised model can be calculated. It cannot. The generated history has no investigation notes and no eligible reviewed outcomes. Rule triggers, anomaly ranks, and scenario codes are not used as substitutes. The command does not write a label or a score, and it does not report precision or recall.

`python -m amlog.benchmark.run` loads a separate benchmark into database `amlog_benchmark`. Histories go through the existing ingestion path and the existing R001 and R002 replay. An outcome is stored only when that replay creates an alert and the existing feature code writes a snapshot. The outcome comes from a support finding dated after the alert. The Phase 10A payment dossiers are not used as alerts. The command does not train a model and does not report accuracy. The design, the case counts, and the limitations are in `docs/benchmark-design.md`. A clone does not include that database.

## SynthAML research benchmark

The business objective is to prioritize alerts an existing monitoring system has already raised. A supervised model uses historical investigation outcomes to put more report-worthy alerts earlier in the queue, at the same investigator capacity.

On the published SynthAML extract the logistic baseline trained on 10,839 alerts from 2020 and scored 9,161 alerts from 2021. At 5, 10, and 20 percent capacity it identified 127, 257, and 538 Report alerts, against 75, 152, and 297 for published date order. Precision at those capacities was 0.277293, 0.280568, and 0.293668, against 0.163755, 0.165939, and 0.162118. Recall was 0.078106, 0.158057, and 0.330873, against 0.046125, 0.093481, and 0.182657. At 20 percent capacity, 1,088 Report alerts remained outside that review.

A later comparison used only 2020 forward quarters to choose among logistic regression, a random forest, histogram gradient boosting, extra trees, and an unweighted score average. The primary metric was Report outcomes captured at 10 percent capacity. Random forest led that validation with 246, against 230 for logistic regression, 233 for the average, and 141 for date order. After the choice was locked, the same forest scored 2021 and identified 122, 266, and 541 Report alerts at 5, 10, and 20 percent. At 10 percent that is 9 more than the logistic baseline and 114 more than date order. At 5 percent it is 5 fewer than the logistic baseline. The 2021 labels had already been inspected for the baseline, and they were not used to change the choice. These figures are results on that synthetic benchmark, not measured results for a financial institution. The selected forest is not approved for operational use.

The dataset is SynthAML, Jensen et al., *Scientific Data* (2023), [10.1038/s41597-023-02569-2](https://doi.org/10.1038/s41597-023-02569-2). The alert and transaction files are [10.6084/m9.figshare.22396054.v1](https://doi.org/10.6084/m9.figshare.22396054.v1) and [10.6084/m9.figshare.22396057](https://doi.org/10.6084/m9.figshare.22396057). The paper is CC BY 4.0 and the data files are CC0.

Reproduce the saved benchmark, without opening an AMLOG database:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.synthaml.run
.\.venv\Scripts\python.exe -m amlog.synthaml.select_run
```

Demonstrate it from the existing review application. The investigator queue stays at `/`. The research view is read-only:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.review.serve --database amlog_workload_integrated --port 8765
```

Open `http://127.0.0.1:8765/synthaml`. The page reads `var/synthaml/synthaml-phase11-evaluation.json` and, when present, `var/synthaml/synthaml-phase11c-selection.json`. It does not fit a model again. Raw CSV files and those artifacts stay outside git. The design and the full tables are in `docs/synthaml-benchmark.md`.

The score estimates the relative likelihood of a Report outcome on that synthetic benchmark. The operational supervised-training gate remains closed.

## What is not in this repository

- `.env` and any database password
- the Docker volume that holds the local databases
- fitted model files under `var/model-artifacts` and `var/synthaml`
- the raw SynthAML CSV files
- generated payment files, customer extracts, or score exports
- an approved production model, an operational supervised false-positive model, or an AWS deployment

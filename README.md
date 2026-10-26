# AMLOG

AMLOG is a client-neutral foundation for prioritizing anti-money laundering alerts and governing the models that support that work. It ingests payment events, stores them in PostgreSQL, replays two versioned monitoring rules, builds a point-in-time feature snapshot for each alert, ranks those alerts with an experimental anomaly model, and lets a person review the same alert on a local page.

The ranking is not approved for production. It is not a probability of financial crime, a productive investigation, or a false positive. No supervised false-positive model has been trained. No cloud resources have been created. This repository does not claim a client deployment, measured operational performance, or regulatory certification.

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

## What is not in this repository

- `.env` and any database password
- the Docker volume that holds the local databases
- fitted model files under `var/model-artifacts`
- generated payment files, customer extracts, or score exports
- an approved production model, a supervised false-positive model, or an AWS deployment

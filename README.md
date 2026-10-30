# AMLOG — AML Detection Optimization & Model Governance

A financial-services or digital-payments business that already runs rules-based anti-money laundering (AML) monitoring still receives a large alert queue. Many of those alerts arise from legitimate financial activity. Investigators must review them, and the number of people available to do that work is limited. An AI-assisted ranking can place alerts that are more likely to produce a meaningful investigative outcome earlier in the queue, at the same review capacity. The investigator still decides. Regulatory controls, model approval, and an audit trail stay in place.

AMLOG is an enterprise AI and data-architecture reference implementation for that problem. It adds intelligent alert prioritization, an investigation workspace, and model governance above monitoring that has already raised an alert. It is not a claim that a named institution has deployed it, and it does not replace the human decision.

## Measured Research Results

The headline comparison uses the public SynthAML research extract, defined in the supervised section below. It contains 20,000 previously raised alerts and 16,232,955 linked transactions. Algorithm selection used the 2020 alerts only. The locked model was then scored once on the 2021 evaluation population of 9,161 alerts. Full comparison tables and the selection protocol are in [docs/synthaml-benchmark.md](docs/synthaml-benchmark.md).

Random forest, selected on 2020 validation, against published date order on the 2021 alerts:

| Investigator capacity | Alerts reviewed | Date order, Report outcomes | Random forest, Report outcomes |
| --- | ---: | ---: | ---: |
| 5% | 458 | 75 | 122 |
| 10% | 916 | 152 | 266 |
| 20% | 1,832 | 297 | 541 |

At 10 percent capacity the random forest captured 114 more Report outcomes than date order. That is a 75 percent relative increase over the 152 outcomes captured by date order in this research benchmark. It is not model accuracy, and it is not a measured reduction in operational false positives.

At that same 10 percent capacity, precision is 16.59 percent for date order and 29.04 percent for the random forest. Those percentages are the documented rates 0.165939 and 0.290393. Recall is 9.35 percent and 16.36 percent, the documented rates 0.093481 and 0.163592. Recall uses the 1,626 Report alerts in the 2021 population.

The gain is not the same at every capacity. At 5 percent the forest captured 122 Report outcomes, five fewer than the logistic regression baseline, which captured 127. At 20 percent an unselected equal-score ensemble captured 551, ten more than the forest. Those 2021 differences were not used to change the model that 2020 validation had already locked. The tree models also show an overfitting gap: training precision at 10 percent on the 2020 folds is 0.982 to 1.000, while validation precision at 10 percent is 0.211 to 0.331. Selection used the validation counts.

## Implemented capabilities

AMLOG implements two separate paths. The first is a local monitoring and investigation platform on generated payment histories. The second is a supervised research evaluation of alerts that an external monitoring system has already raised.

### Local AML monitoring and investigation platform

This path demonstrates end-to-end processing on the workstation:

- Payment ingestion with source-event idempotency. An identical delivery is acknowledged. A changed delivery is rejected.
- PostgreSQL storage for customers, accounts, transactions, alerts, features, scores, model versions, and investigation records.
- Versioned monitoring rules R001 (outgoing velocity) and R002 (rapid funds turnover). Each rule's thresholds are stored on its own version.
- Alert-level behavioral features, feature set `alert_behavior` version 1, computed only from that alert's account and from payments at or before the alert time.
- Experimental anomaly ranking with an Isolation Forest and a robust z-score baseline. Both scores are experiment scores.
- A local investigator page for the alert, its payments, its features, the experiment rank, case notes, a usefulness assessment, and the case history.
- Model versioning, feature snapshots, and score lineage. An experiment score requires a validated model version. An operational score requires an approved model version.

R001 and R002 exist so the local platform can raise alerts and carry them through features, ranking, and review. They are a demonstration of that processing path. They are not the differentiator against an existing AML vendor platform. The business capability is what happens after an alert already exists.

### Supervised AI alert prioritization

SynthAML is a public synthetic research dataset published by Jensen et al. in *Scientific Data* (2023). In plain terms, it supplies alerts that a monitoring system has already raised, the transaction history linked to each alert, and a research outcome of Report or Dismiss. AMLOG does not create those alerts and does not replay R001 or R002 over them. Report and Dismiss are kept out of the feature matrix and out of the operational training gate.

The supervised question is whether a model trained on earlier outcomes can place more Report alerts inside a fixed investigator capacity than reviewing the same alerts in published date order.

Feature set `synthaml_alert_history` version 1 has 56 statistics of the published transaction size, by entry and by payment type. Outcome is joined only after that matrix exists. The positive class is Report.

Five candidates were compared. Random forest was not chosen in advance:

- Regularized logistic regression, retained as the baseline
- Random forest
- Histogram gradient boosting
- Extra trees
- An unweighted score-level ensemble, the mean of the four model scores, with no fitted weights

Selection used only 2020 alerts, in forward quarters: train on earlier quarters and validate on the next quarter. The primary metric, declared before the fits, was the number of Report outcomes captured at 10 percent investigator capacity. On that metric the random forest led, with 246 Report outcomes across the validation quarters, against 233 for the ensemble, 232 for extra trees, 230 for logistic regression, 211 for histogram gradient boosting, and 141 for date order. The forest was locked for that reason. It was then fit on all 10,839 alerts from 2020 and scored once on 2021. The 2021 outcomes had already been inspected for the logistic baseline, and they were not used to change the choice.

The dataset citation is SynthAML, Jensen et al., *Scientific Data* (2023), [10.1038/s41597-023-02569-2](https://doi.org/10.1038/s41597-023-02569-2). The alert and transaction files are [10.6084/m9.figshare.22396054.v1](https://doi.org/10.6084/m9.figshare.22396054.v1) and [10.6084/m9.figshare.22396057](https://doi.org/10.6084/m9.figshare.22396057). The paper is CC BY 4.0 and the data files are CC0.

## Solution concept

The target business workflow sits on top of monitoring that an institution already operates:

```text
Existing AML monitoring system
  -> existing alerts
  -> behavioral feature engineering
  -> supervised alert prioritization
  -> investigator review
  -> investigation outcomes and feedback
  -> model governance and monitoring
```

The repository implements the pieces of that workflow, and it does not join them into one production pipeline.

- The local platform generates payment histories, replays R001 and R002, builds `alert_behavior` features, and ranks alerts with an experimental Isolation Forest and a robust z-score. Investigators can record notes and assessments on that local queue. Those assessments are not eligible training labels.
- The SynthAML adapter is a separate research evaluation. It ranks previously raised public alerts with a supervised model and reports precision and recall only for that benchmark.
- No operational supervised model is trained on AMLOG investigator labels. The command that would do so stays blocked until eligible reviewed outcomes meet the declared floor.

Design records are [docs/business-requirements.md](docs/business-requirements.md), [docs/solution-architecture.md](docs/solution-architecture.md), [docs/database-design.md](docs/database-design.md), [docs/data-methodology.md](docs/data-methodology.md), [docs/benchmark-design.md](docs/benchmark-design.md), and [docs/synthaml-benchmark.md](docs/synthaml-benchmark.md).

## Model governance

AMLOG separates an experiment score from an operational score. A model version moves through registration and validation. Operational use requires an approval that this repository does not grant. The Isolation Forest and the robust z-score baseline are validated for experiment scores only. `approved_by` is empty. The SynthAML random forest is stored as an experiment artifact with `approved_for_operational_use` false. It is not written into the operational model register, and it does not open the supervised-training gate.

Feature snapshots record which inputs were visible at the alert time. Scores record the model version that produced them. A repeated ranking does not insert a second score for the same result. Investigation notes, usefulness assessments, and case dispositions are stored as their own records, so a later reader can see who recorded what. The reviewer name on the local page is an audit reference. It is not authentication.

Labels, scenario notes, investigation outcomes, rule codes, and record identifiers are not inputs to the local anomaly ranking. Amounts are scaled inside one currency. A currency with too little training history is left missing rather than compared with another currency.

## Data Confidentiality & Project Disclaimer

AMLOG is an independent, client-neutral reference implementation demonstrating enterprise Anti-Money Laundering (AML) detection, alert prioritization, and AI/ML model governance capabilities.

In accordance with client confidentiality principles, organizational data protection policies, and proprietary information safeguards, no real client transaction data, confidential business records, or proprietary organizational workflows have been used in this implementation.

The project uses synthetic datasets, including independently generated demonstration workloads and publicly available synthetic research datasets. Customer identities, transaction patterns, business scenarios, and operational workflows are illustrative and do not represent any specific client's actual systems or business operations.

This repository is intended exclusively for architectural demonstration, technical experimentation, and research evaluation. It does not represent a production deployment or an implementation for any identified financial institution. Reported results reflect the documented experimental conditions and should not be interpreted as production performance.

## Evaluation scope and limitations

The measurements above are reproducible research and development results. They are not the performance of a live financial institution, and they are not regulatory certification.

- The local workloads are generated. The 2,000-payment demonstration and the 10,000-payment integrated population are fictional histories. They are not a customer extract.
- Isolation Forest agreement with the robust z-score baseline is a rank correlation on withheld accounts. It is not precision, recall, or ROC-AUC, and it is not a business saving.
- `python -m amlog.prioritize.supervised_run` fits a classifier only when eligible reviewed outcomes exist. An outcome counts when `eligible_for_evaluation` is true, the source is `investigator_outcome` or `quality_review`, and the latest value is `productive_alert` or `non_productive_alert`. The floor is 25 alerts in each class and two reviewers. The generated workloads have none of those outcomes, so the command stops with `BLOCKED`. A usefulness assessment entered on the review page remains ineligible for training.
- `python -m amlog.evaluate.run` checks whether a rules-only queue, the anomaly rank, and a supervised model can be compared on eligible outcomes. On the generated history that comparison cannot be calculated. The command does not write a label or a score.
- A separate controlled benchmark can be loaded with `python -m amlog.benchmark.run` into database `amlog_benchmark`. Its case counts and limits are in [docs/benchmark-design.md](docs/benchmark-design.md). It does not train a model. A clone does not include that database.
- SynthAML Report and Dismiss outcomes are research labels. A Dismiss does not prove that no financial crime occurred. A Report is not a conviction or an AMLOG investigator finding. Published dates are reliable to the quarter. The synthesizer was fit on the full extract, including alerts this experiment holds out by year. The 2021 population is the final evaluation split, and it is not an untouched holdout for the whole project, because those outcomes were inspected for the logistic baseline before model selection.
- No cloud resources are created by this repository. Nothing here is an approved production model.

## Local setup

Python 3.11 or newer is required. PostgreSQL 16 runs locally through Docker Desktop and Docker Compose, bound to `127.0.0.1` only.

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

These commands are the local path from an empty database to a review page. They generate a fictional 2,000-payment workload in `amlog_workload`, using the loader default of 2,000 events, seed `20261025`, and population id `local-validation`. They do not load a customer or payment extract. Workload, rule, feature, and ranking commands refuse the development database named by `AMLOG_DATABASE_URL`. `--reset` drops only the disposable database named by `--database`.

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

The review page is `http://127.0.0.1:8765/`. Sort with `?sort=priority` to see the anomaly order. Entering a reviewer reference is not authentication. The page does not train a model. On this generated workload, `supervised_run` is expected to stop with `BLOCKED`.

The 2,000-payment commands do not reproduce the 10,000-payment figures in the next section. Those figures come from a different disposable database, `amlog_workload_integrated`, loaded with `--events 10000`, `--seed 20261026`, and `--population integrated`. A clone does not include that database, its scores, or the fitted artifact files. Running the smaller demonstration builds another population, so its alert counts and rank agreement will differ.

## Integrated local workload

These figures describe one generated population in the local database `amlog_workload_integrated`. They describe that fictional history. They are separate from the SynthAML research results above.

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

To inspect that integrated queue, start the review application against its database:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.review.serve --database amlog_workload_integrated --port 8765
```

## Reproduce the SynthAML benchmark

The research commands do not open an AMLOG database. If the published CSV files are absent, the commands download them to a cache outside the repository. Raw files and fitted artifacts stay outside git.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.synthaml.run
.\.venv\Scripts\python.exe -m amlog.synthaml.select_run
```

`amlog.synthaml.run` writes the logistic regression baseline. `amlog.synthaml.select_run` runs the 2020 comparison, locks the selected model, scores 2021, and leaves the baseline files in place.

The same local review application serves the research view. The investigator queue stays at `/`. The research view is read-only:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.review.serve --database amlog_workload_integrated --port 8765
```

Open `http://127.0.0.1:8765/synthaml`. The page reads `var/synthaml/synthaml-phase11-evaluation.json` and, when present, `var/synthaml/synthaml-phase11c-selection.json`. It does not fit a model again. The score is a research ranking for a Report outcome on that benchmark.

## What is not in this repository

- `.env` and any database password
- the Docker volume that holds the local databases
- fitted model files under `var/model-artifacts` and `var/synthaml`
- the raw SynthAML CSV files
- generated payment files, customer extracts, or score exports
- an approved production model or an AWS deployment

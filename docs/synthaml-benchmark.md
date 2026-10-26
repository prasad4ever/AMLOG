# SynthAML research benchmark

The business objective is to prioritize alerts that an existing monitoring system has already raised. Historical investigation outcomes teach a supervised model which of those alerts were reported. At the same review capacity, the question is whether that ranking surfaces more report-worthy alerts than reviewing them in published date order.

Experiment `phase11-synthaml-v1` is the logistic regression baseline. Experiment `phase11c-synthaml-v1` compares that baseline with three other candidates on 2020 quarters only, then scores 2021 once. The local review page reads the saved artifacts and does not fit a model again.

## Attribution

SynthAML, Jensen et al., *Scientific Data* (2023), DOI `10.1038/s41597-023-02569-2`. The paper is CC BY 4.0.

Alert file: DOI `10.6084/m9.figshare.22396054.v1`, `synthetic_alerts.csv`. Transaction file: DOI `10.6084/m9.figshare.22396057.v1`, `synthetic_transactions.csv`. Both data files are CC0.

## Reproduce the benchmark

From the repository root, with the project virtual environment:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.synthaml.run
```

The command uses the published files from the local cache outside the repository, or downloads them there if they are absent. It writes `var/synthaml/synthaml_report_logistic-1.joblib` and `var/synthaml/synthaml-phase11-evaluation.json`. Both paths are gitignored. It does not open an AMLOG database, does not run R001 or R002, and does not write an investigator label.

The model comparison reuses that feature builder. It does not regenerate the extract:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.synthaml.select_run
```

That command writes `var/synthaml/synthaml-phase11c-selected.joblib` and `var/synthaml/synthaml-phase11c-selection.json`. It leaves the logistic baseline files in place.

## Demonstrate the research view

Start the existing local review application against the integrated workload:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m amlog.review.serve --database amlog_workload_integrated --port 8765
```

Open `http://127.0.0.1:8765/` for the investigator queue. Open `http://127.0.0.1:8765/synthaml` for the read-only SynthAML Research Benchmark. The research page shows the saved population, the selected experimental model when `synthaml-phase11c-selection.json` is present, and the logistic regression baseline from `synthaml-phase11-evaluation.json`. It has no form. Research outcomes are not written to `amlog_label.alert_labels`.

The figures from this command are results on that synthetic benchmark. They are not measured results for a financial institution. The model is not approved.

## What the adapter treats as given

SynthAML alerts are alerts that an external monitoring system has already raised. AMLOG ranks those existing alerts. It does not generate them.

The alert file has `AlertID`, `Date`, and `Outcome`. Outcome is `Report` or `Dismiss`. The transaction file has `AlertID`, `Timestamp`, `Entry`, `Type`, and `Size`, and links to alerts through `AlertID`.

`Entry` stays as published. Credit is a transaction that decreases the client's balance. Debit is a transaction that increases it. Those words are not AMLOG payment directions.

`Size` stays as published. It is a log-transformed Danske kroner amount, standardized by the dataset authors. It is not an AMLOG currency amount.

## Features

Feature set `synthaml_alert_history` version `1` has 56 columns: minimum, mean, median, maximum, population standard deviation, count, and sum of `Size`, for each of Card, Cash, International, and Wire, and for each of Credit and Debit.

A type and entry with no transactions has count 0. The other six statistics are the published sentinel `-3`. That sentinel is kept. It is not replaced by a training median.

The builder reads alert dates and transaction rows. It has no outcome argument. A transaction whose calendar date is after the alert's published date is excluded from the matrix, and the fit stops.

Population standard deviation divides by the number of transactions in the group. One transaction has standard deviation 0.

## Split and model

Training rows are alerts dated in 2020. Held-out rows are alerts dated in 2021. The boundary is 1 January 2021, which is a quarter boundary of the kind the authors require. Dates inside a quarter were randomized by the authors, so the experiment does not cut the data inside a quarter.

Column mean and population standard deviation are fit on the 2020 rows only. A training column with no variation keeps scale 1. The 2021 rows are transformed with that scale.

The Phase 11 baseline is `synthaml_report_logistic` version `1`, scikit-learn logistic regression, L2 penalty, `C=1`, solver `lbfgs`, `max_iter=1000000`, seed `20261026`. The positive class is Report. The score is the estimated relative likelihood of Report on this benchmark. It is not a probability of financial crime, a conviction, or an operational AMLOG label.

Those logistic settings are the declared baseline. They were not searched against the 2021 alerts. The later comparison keeps those same settings for the logistic candidate.

## Comparison

Two orders review the same 2021 alerts:

- Unprioritized order: published `Date`, then numeric `AlertID`. Within a quarter, that date is a random date from the true quarter, so this is not the bank's original within-quarter queue.
- Supervised order: descending Report score, then ascending `AlertID`.

Capacity is `(test alerts * percent) // 100` at 5, 10, and 20 percent. Those three capacities were fixed before scoring. Both orders review that same count. At each capacity the report records alerts reviewed, Report alerts identified, precision, recall, Report alerts left outside the capacity, and the supervised count minus the unprioritized count.

## Limitations

- Published alert dates are accurate to the quarter. Within-quarter order is randomized.
- The synthesizer was fit on the full extract, including alerts that this experiment holds out by date.
- There is no customer or account identifier, so repeated clients cannot be pinned to one side of the split.
- About 17 percent of alerts are Report. The paper cites live false-positive rates of 95 to 98 percent, so this benchmark is less imbalanced than those live queues.
- Report and Dismiss are investigation outcomes. A Dismiss outcome does not establish that no financial crime occurred. A Report outcome is not a conviction.

Raw CSV files stay outside the repository. The fitted model and the evaluation document are written under `var/synthaml`, which is gitignored.

## Measured results

Local run of `python -m amlog.synthaml.run`. scikit-learn 1.7.2. The logistic regression converged in 99 iterations. Feature aggregation took 133.225 seconds. Fitting and scoring took 0.235 seconds. The whole command took 133.673 seconds. The transaction file was grouped by AlertID, so it was aggregated one alert at a time. Rows after the published alert date: 0. Rows more than 365 days before the alert date: 0.

| Population | Alerts | Report | Dismiss |
| --- | ---: | ---: | ---: |
| 2020 training | 10,839 | 1,809 | 9,030 |
| 2021 held-out test | 9,161 | 1,626 | 7,535 |
| Full extract | 20,000 | 3,435 | 16,565 |

Transactions: 16,232,955. The 2021 report share is 1,626 / 9,161.

Review capacity is `(9161 * percent) // 100`. Both orders review that many alerts. Precision and recall below are Report alerts identified divided by alerts reviewed, and by the 1,626 test Report alerts. The supervised difference is the supervised Report count minus the date-order Report count.

| Capacity | Reviewed | Date order reports | Supervised reports | Date order precision | Supervised precision | Date order recall | Supervised recall | Date order remaining | Supervised remaining | Supervised minus date order |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5% | 458 | 75 | 127 | 0.163755 | 0.277293 | 0.046125 | 0.078106 | 1,551 | 1,499 | +52 |
| 10% | 916 | 152 | 257 | 0.165939 | 0.280568 | 0.093481 | 0.158057 | 1,474 | 1,369 | +105 |
| 20% | 1,832 | 297 | 538 | 0.162118 | 0.293668 | 0.182657 | 0.330873 | 1,329 | 1,088 | +241 |

At each declared capacity, the supervised rank identifies more Report alerts than published date order. At 20 percent capacity it identifies 538 of 1,626 Report alerts, and 1,088 Report alerts remain outside that review capacity.

The unprioritized precision stays near the 17.7 percent test report share. That is what date order produces when dates inside a quarter were randomized. The supervised precision is 27.7 percent, 28.1 percent, and 29.4 percent at the three capacities.

These figures are results on the SynthAML research benchmark. They are not measured results for a financial institution. The logistic artifact and the full score list are under `var/synthaml`, which is gitignored. The model is not approved, and the operational supervised-training gate does not read these outcomes.

The 2021 outcomes in this table were inspected for this baseline before the candidate comparison. The comparison below does not treat 2021 as an untouched holdout for the whole project.

## Model selection

The business objective of the comparison is the same: at a fixed investigator capacity, capture more Report outcomes. The primary selection metric, declared before the fits, is the total number of Report outcomes inside a 10 percent review of each forward validation quarter. Precision and recall at 5, 10, and 20 percent, PR-AUC, and the spread of precision at 10 percent are secondary. Overall accuracy is not used.

Only 2020 alerts are used to choose the algorithm and the declared settings. The folds respect the authors' quarter boundaries:

- Train on 2020 Q1 and validate on 2020 Q2.
- Train on 2020 Q1–Q2 and validate on 2020 Q3.
- Train on 2020 Q1–Q3 and validate on 2020 Q4.

Column mean and population standard deviation are fit on the training side of each fold. The validation quarter is not in that fit. A candidate within 10 Report outcomes of the leader is treated as equally effective. Inside that band, the stabler precision at 10 percent wins, and then the simpler candidate. The equal-score ensemble is the unweighted mean of the four member scores. Its weights are not fit.

Declared settings, seed `20261026`, scikit-learn 1.7.2:

- Logistic regression: the Phase 11 baseline, without class weights.
- Random forest: 200 trees, depth at most 16, at least 5 training alerts per leaf, `class_weight='balanced'`, one job.
- Extra trees: the same tree limits and class weights.
- Histogram gradient boosting: 200 iterations, learning rate 0.1, depth at most 6, early stopping off, and class weights computed from the training rows of that fold only.

No other hyperparameter combinations were tried.

Validation quarters contain 2,047, 3,109, and 3,502 alerts, with 345, 491, and 586 Report outcomes. A 10 percent review is 204, 310, and 350 alerts. The local selection run spent 77.725 seconds building the existing 56-feature matrix and 46.405 seconds on the folds and the later 2021 scores. Total 124.130 seconds.

| Candidate | Q2 reports at 10% | Q3 reports at 10% | Q4 reports at 10% | Total at 10% | Total at 5% | Total at 20% | Precision at 10%, standard deviation | Mean PR-AUC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Date order | 42 | 39 | 60 | 141 | 75 | 293 |  |  |
| Logistic regression | 52 | 75 | 103 | 230 | 117 | 453 | 0.022260 | 0.235792 |
| Random forest | 49 | 81 | 116 | 246 | 118 | 468 | 0.038998 | 0.243272 |
| Histogram gradient boosting | 46 | 67 | 98 | 211 | 108 | 400 | 0.028163 | 0.214184 |
| Extra trees | 43 | 73 | 116 | 232 | 121 | 435 | 0.052037 | 0.227865 |
| Equal-score ensemble | 51 | 72 | 110 | 233 | 117 | 443 | 0.035239 | 0.234621 |

Random forest is the selected candidate. It captured 246 Report outcomes at 10 percent, 16 more than logistic regression and 105 more than date order. The next candidate, the ensemble, captured 233, which is 13 outside the predeclared band of 10. The simplicity rule therefore did not apply. Every fitted candidate beat date order on this primary total.

The alternatives stay unselected for these measured reasons:

- Logistic regression captured 230. It is simpler, and its precision at 10 percent is more stable across the three quarters (standard deviation 0.022260 against 0.038998). On Q2, with only Q1 available for training, it captured 52 against the forest's 49. The declared band still leaves it unselected because it is 16 reports behind the leader.
- Extra trees captured 232. At 5 percent it captured 121, three more than the forest, but 5 percent is not the selection metric. At 20 percent it captured 435, behind both the forest and logistic regression. Its precision at 10 percent is the least stable of the candidates (standard deviation 0.052037).
- Histogram gradient boosting captured 211, behind logistic regression on every validation quarter.
- The equal-score ensemble captured 233. It did not beat the best single model, so the extra inference cost is not justified by the validation rule.

The tree models fit the training side of each fold very tightly. Training precision at 10 percent is 0.982 to 1.000, while validation precision at 10 percent is 0.211 to 0.331. Logistic regression does not show that gap: its training precision at 10 percent is 0.417, 0.348, and 0.297 against validation precision 0.255, 0.242, and 0.294. The forest's selection uses the validation counts, which exclude those training rows. The training gap is still a sign that the forest memorizes the quarters it fits.

After that choice was locked, each declared candidate was fit once on all 10,839 alerts from 2020 and scored once on the 9,161 alerts from 2021. The 2021 labels were not used to change the choice. Those labels had already been inspected for the Phase 11 logistic baseline. The refit logistic candidate reproduces that baseline exactly: 127, 257, and 538 Report outcomes at 5, 10, and 20 percent.

| Candidate | Reports at 5% | Reports at 10% | Reports at 20% | Precision at 10% | Recall at 10% | PR-AUC | Reports remaining at 10% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Date order | 75 | 152 | 297 | 0.165939 | 0.093481 | 0.174576 | 1,474 |
| Logistic regression baseline | 127 | 257 | 538 | 0.280568 | 0.158057 | 0.255449 | 1,369 |
| Random forest, selected | 122 | 266 | 541 | 0.290393 | 0.163592 | 0.257920 | 1,360 |
| Histogram gradient boosting | 118 | 251 | 502 | 0.274017 | 0.154367 | 0.241840 | 1,375 |
| Extra trees | 124 | 251 | 518 | 0.274017 | 0.154367 | 0.249251 | 1,375 |
| Equal-score ensemble | 123 | 245 | 551 | 0.267467 | 0.150677 | 0.255803 | 1,381 |

On the primary 2021 capacity the selected forest identifies 266 Report outcomes, 9 more than the logistic baseline and 114 more than date order. Precision at 10 percent is 0.290393 and recall is 0.163592. PR-AUC is 0.257920 against 0.255449 for logistic regression and 0.174576 for date order. The gain is small. It is not uniform: at 5 percent the forest identifies 122, which is 5 fewer than logistic regression, and at 20 percent the unselected ensemble identifies 551, which is 10 more than the forest. Those 2021 differences were not allowed to change the locked choice.

The saved forest is about 18.9 MB, 200 trees with the limits above. Fitting and scoring the 2021 comparison took 18.173 seconds for the forest and 0.173 seconds for logistic regression. Both are practical for this 9,161-alert score. The forest score is an average of tree votes and is harder to read than the 56 logistic coefficients. The first saved 2021 rank is AlertID 17262, score 0.666122, Report. Six of the next seven saved ranks are Dismiss. The measured gain is the count inside the 916-alert window, not a claim that the first alerts are reports.

The selected model remains an experiment. `approved_for_operational_use` is false. It is not an operational model version, and it does not open the supervised-training gate. Reloading `synthaml-phase11c-selected.joblib` reproduces the saved top score without fitting again.

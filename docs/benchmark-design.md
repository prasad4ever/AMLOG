# Investigation-outcome benchmark

Active design: `phase10b-alert-v1`. Database: `amlog_benchmark`.

The judgments are protocol results. They are not investigator findings, not verified financial crime, and not operational training labels. No model is trained from them, and no prediction accuracy is reported.

## Phase 10A dossiers

Design `phase10a-dossier-v1` built 192 payment dossiers. `amlog.benchmark.phase10a` applies the published rule thresholds to those payments:

| Check | Count |
| --- | --- |
| Dossiers | 192 |
| Meet R001 version 1: 6 debits inside 3,600 seconds | 0 |
| Meet R002 version 1: an incoming amount of at least 500, then outgoing allocation of at least 80 percent inside 24 hours | 0 |

R001 does not fire because a dossier has at most two debits, and those debits are 24 hours apart. R002 does not fire because every incoming amount is below 500. Those dossiers are not rule-generated AML alerts. They are not loaded.

Their Phase 10A outcomes also used beneficiary novelty, the focal amount against the earlier median, and a later payment. The active protocol does not treat those facts as an investigation outcome. The 192 dossiers therefore have no defensible constructed outcome under this design.

## Active judgment

`amlog.benchmark.protocol.judge` reads one support finding obtained after the alert. It does not read payments, a rule code, an anomaly score, a scenario code, or a case disposition.

| Assumption | Judgment | Evidence in the review file |
| --- | --- | --- |
| C1 | `productive_review` | The requested support document was not produced, or the document that was produced does not match the alerted payment on beneficiary, amount, or reference. |
| C2 | `non_productive_review` | A support document was produced and it matches the alerted payment on beneficiary, amount, and reference. |

The recorded actor is `amlog.benchmark.protocol`.

Paying the beneficiary named in a pre-alert arrangement occurs in both outcomes. Paying a different beneficiary, or paying with no arrangement on file, also occurs in both outcomes. R001 and R002 each occur in both outcomes.

## Alert and feature linkage

Each history is ingested with the existing payment ingestor. `run_r001_replay` and `run_r002_replay` create the alerts. `run_feature_snapshots` writes `alert_behavior` version 1. A judgment is stored only when all of the following are true:

- the account has exactly one alert
- that alert's triggering payment is the subject payment named in the case file
- the alert's rule is the rule the history was built to exercise
- exactly one feature snapshot exists for that alert
- recomputing the snapshot with the account's later payments does not change the fingerprint

The mapping is `amlog_benchmark.case_files` to `amlog_benchmark.alert_links` to `amlog.alerts` and `amlog.feature_computations`, then `amlog_benchmark.judgments`. A judgment row cannot exist without a link.

Every history also contains a payment after the alert time. That payment remains on the account and is outside the snapshot.

## Counts

| Population | Cases | Productive | Non-productive | Alerts and snapshots |
| --- | --- | --- | --- | --- |
| `benchmark-fit` | 8 | 4 | 4 | 8 |
| `benchmark-holdout` | 8 | 4 | 4 | 8 |
| Both | 16 | 8 | 8 | 16 |

All 16 specified histories linked. All 16 have a usable snapshot. All 16 have a protocol outcome.

Fit and holdout accounts are disjoint. No payment amount is shared across the two populations. No holdout history repeats a fit history's direction and channel sequence.

Each context family has 2 independent cases in each population. That is a small slice, not a population estimate. The families are `merchant_settlement`, `established_supplier`, `unexplained_transfer`, and `new_counterparty`. They are stored for later evaluation slices and are not predictive columns.

## Predictive inputs

The export is the `alert_behavior` snapshot except `rule_code`, `rule_version`, `evidence_count`, `evaluation_event_time`, `availability_basis`, and `amount_currency`, plus two pre-alert attributes: `customer_type` and `arrangement_on_file`.

The snapshot itself still stores the rule code, because that is what the existing feature component writes. The benchmark export leaves it out.

The support finding, the outcome, the rationale, and the context family are not in the export. The arrangement beneficiary token is stored on the case file as a pre-alert fact and is not in the export.

## What this benchmark cannot support

- The 192 Phase 10A dossiers cannot be reported as alert-linked cases.
- Eight productive and eight non-productive judgments are below the operational gate of 25 in each class. The histories were not expanded to reach that floor.
- Two cases in a family are not enough for a family-level performance claim.
- Agreement with C1 and C2 would measure agreement with this protocol. It would not measure investigator time saved.
- The judgments are not eligible `investigator_outcome` or `quality_review` labels. The supervised gate stays blocked.

## Readiness

A controlled supervised experiment is not ready. The linkage from case to alert to feature snapshot to protocol outcome is in place for a later experiment. The independent case count is not.

# Business requirements

## Purpose

AMLOG is a foundation for an anti-money laundering (AML) alert-optimization platform. The platform is intended to help a financial institution spend investigator time on the alerts that most deserve review, while keeping a durable record of how each alert, rule version, model version, and score was produced.

This document states that objective, the capabilities implemented in this repository, and the capabilities still ahead. It does not describe a live client deployment.

## Problem

Rules-based transaction monitoring creates alerts faster than investigators can review them with equal attention. A useful platform must:

- accept payment activity from more than one channel
- keep each alert tied to the rule version that created it
- rank or otherwise prioritize alerts with a model whose version is known
- show a person why an alert was prioritized
- give investigators a case file that cannot silently lose its history
- keep later judgments about alert quality out of the scoring inputs
- leave an audit trail that another person can follow

The repository ingests payment events, replays two versioned monitoring rules over generated history, stores one feature snapshot per alert, writes an experimental anomaly rank, and records a human review on a local page. It does not run live monitoring, and it does not approve a model for production. The working rank orders alerts by how unusual they look. Supervised prediction of which investigations will be productive remains unvalidated, because no eligible reviewed outcomes are available.

## People who will use the platform

These are roles, not named users or a named institution.

| Role | Responsibility |
| --- | --- |
| Investigator | Reviews alerts, writes case notes, and records how a case ended. |
| Monitoring analyst | Authors and retires rule versions. |
| Model steward | Registers model versions, records where training data came from, and approves or rejects a version. |
| Platform operator | Runs intake, migrations, and access control. The operator is not a substitute for the model steward. |

## Capabilities

### 1. Multi-channel payment data integration

The platform must accept activity from many source systems and payment channels without reserving a fixed list of channels in the database. The same source event, if delivered more than once, must not become two transactions. The ingestion function accepts `payment_event_received` schema 1 and schema 2, resolves the account, and stores one transaction. No external payment source is connected.

### 2. Rules-based AML alert generation

An alert must cite one exact rule version. A published rule version must stay as it was. A change in rule behavior is a new version. Two replayed rules are implemented: R001, outgoing velocity, and R002, rapid funds turnover. There is no general rule language.

### 3. Behavioral feature engineering

Features describe behavior of customers, accounts, counterparties, and payment channels. The platform must not impose a fixed maximum number of features. A feature value must remain attached to the alert snapshot that produced it. Feature set `alert_behavior` version 1 stores one snapshot per alert from that account's payments at or before the alert time.

### 4. ML-based alert prioritization

The experiment rank uses an Isolation Forest and a robust z-score baseline. Neither is approved for operational scoring. The rank orders alerts by how unusual they look relative to the alerts used to fit the model. It is not a probability of financial crime, a productive investigation, or a false positive. Supervised false-positive prediction is a separate later model. It remains unvalidated because no eligible reviewed productive and non-productive outcomes are available. A score must name the model version, the feature snapshot, the meaning of the number, and whether the score is for operations or for an experiment. The feature snapshot must use the exact feature-set name and version required by that model version. An operational score must come from an approved model version.

### 5. Investigation case management

Investigators need a case for a customer, a way to attach alerts, a current status, and notes that remain after they are written. Closing a case requires an operational outcome. That outcome is not, by itself, a training label. A local page bound to `127.0.0.1` can open one case, append notes, record whether the alert was useful to review, close with a separate operational outcome, and reopen the case. A usefulness assessment is not eligible for model training. Entering a reviewer reference is not authentication.

### 6. AI model governance and auditability

Every model version must record its training-data provenance before it exists. Version identity and provenance cannot be edited afterward. Approval is a separate, attributed step. Scores copy the version identity known at scoring time. Changes to master data, cases, and scores are meant to be accompanied by an audit event. The database currently enforces the immutability rules and provides the audit table. It does not yet write an audit row automatically for every business change.

### 7. Near-real-time event processing

Intake should be able to record a transaction soon after a source emits it, and later stages should be able to raise alerts, compute features, and record scores as separate steps. "Near-real-time" is a design goal. This foundation does not state or measure a latency target.

## Scope of this foundation

In scope now:

- business, architecture, data-design, and data-methodology documentation
- a versioned PostgreSQL schema for the entities above
- Python configuration and a migration runner
- payment ingestion for schema 1 and schema 2
- historical replay of R001 and R002
- feature set `alert_behavior` version 1
- an experimental Isolation Forest ranking and a robust z-score baseline, with governed model versions and experiment scores
- a local investigator review page
- tests for the schema, ingestion, rules, features, ranking, review, and governance constraints

Still ahead:

- a live monitor
- a queue or event-bus consumer
- an approved production model
- a supervised false-positive evaluation, until eligible reviewed outcomes exist
- a connected payment source
- any AWS deployment

## Business rules the data structure must protect

- A monetary amount is an exact decimal. It is never a binary floating-point value.
- A timestamp records the civil time with a time zone, not a bare clock reading whose zone is guessed later.
- Debit and credit amounts are stored as a non-negative amount plus a direction.
- The customer on a transaction must be the current owner of the account. Ownership history is not modeled yet.
- An alert and a case may be linked only when they concern the same customer.
- An alert may cite only an active rule version, and the copy of the rule code and version on the alert must match that row.
- Labels used to judge alert quality live in a separate schema. Scoring tables do not reference them.
- A label that comes from synthetic or imported history must say so, and must include a rationale.
- "Productive alert" means the alert was useful to review. It does not mean a crime was proven or that a filing was made.
- Investigator notes are added, not rewritten. A correction is a new note.
- Regulated history is closed by status, not by deleting the row. A development database can be dropped and recreated. Individual history rows cannot.

## Non-functional requirements

| Need | Requirement |
| --- | --- |
| Auditability | Rule versions, model versions, feature snapshots, scores, notes, labels, and audit events remain attributable to the version or person that produced them. |
| Idempotency | One source system plus one source event identifier identifies at most one transaction. |
| Volume | The schema must not cap the number of transactions, alerts, or features. Physical partitioning is a later scale decision, described in the database design. |
| Explainability | The review page shows robust z-score distances from the typical training alert. Those distances are not a causal explanation. An empty explanation is stored as empty rather than invented. |
| Separation of duties | The label schema can be withheld from a future scoring role. Roles are not created in this foundation. |
| Privacy | The customer record holds the legal name and residency country needed to recognize a party. It does not hold government identifiers, birth dates, addresses, or contact details. |
| Operability | Schema changes are numbered SQL files with checksums. An edited applied file is refused. |

## Acceptance for publication review

A publication review can confirm:

- the objective, the implemented capabilities, and the boundary around live monitoring, production approval, and supervised false-positive evaluation
- a normalized schema covering the requested entities, plus the supporting tables named in the database design
- versioned SQL that expresses keys, references, checks, and immutability
- tests for the schema, ingestion, rules, features, ranking, review, and governance constraints
- no claim of a client deployment, measured detection performance, false-positive reduction, or regulatory certification

## Non-claims

This project does not assert that an institution has adopted it, that it has been deployed, that it reduces alert volume by any amount, or that it satisfies a regulator, an examiner, or a certification scheme. The anomaly rank is an experimental ordering. It is not a measured reduction in non-productive investigations.

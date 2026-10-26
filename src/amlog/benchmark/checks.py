"""Checks for the alert-linked benchmark.

The checks cover provenance, coverage, split separation, and the predictive
export. They do not fit a model and they do not calculate accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from amlog.benchmark.histories import CaseSpec, build_cases
from amlog.benchmark.protocol import (
    DISCLAIMER,
    NON_PRODUCTIVE,
    PRODUCTIVE,
    judge,
)

PREDICTIVE_SQL = """
SELECT
    features.feature_name,
    features.value_numeric,
    features.value_text,
    features.value_boolean,
    cases.customer_type,
    cases.arrangement_on_file
FROM amlog.alert_features AS features
JOIN amlog_benchmark.alert_links AS links
    ON links.feature_computation_id = features.feature_computation_id
   AND links.alert_id = features.alert_id
JOIN amlog_benchmark.case_files AS cases ON cases.case_id = links.case_id
WHERE features.feature_name NOT IN (
    'rule_code',
    'rule_version',
    'evidence_count',
    'evaluation_event_time',
    'availability_basis',
    'amount_currency'
)
"""

_EXCLUDED_FEATURE_NAMES = (
    "rule_code",
    "rule_version",
    "evidence_count",
    "evaluation_event_time",
    "availability_basis",
    "amount_currency",
)
_FORBIDDEN_EXPORT_TEXT = (
    "context_family",
    "outcome",
    "assumption_id",
    "rationale",
    "document_kind",
    "finding_note",
    "beneficiary_matches",
    "amount_matches",
    "reference_matches",
    "scenario_code",
)


@dataclass(frozen=True)
class SpecAssessment:
    """Design checks that can run before any database write."""

    passed: bool
    failures: tuple[str, ...]
    notes: tuple[str, ...]
    case_count: int
    fit_productive: int
    fit_non_productive: int
    holdout_productive: int
    holdout_non_productive: int


def assess_cases(cases: tuple[CaseSpec, ...] | None = None) -> SpecAssessment:
    """Check the histories and the support findings before they are loaded."""

    selected = build_cases() if cases is None else cases
    failures: list[str] = []
    notes: list[str] = []
    failures.extend(_provenance(selected))
    failures.extend(_coverage(selected, notes))
    failures.extend(_separation(selected))
    failures.extend(_finding_is_independent(selected))
    failures.extend(_export_boundary())
    return SpecAssessment(
        passed=not failures,
        failures=tuple(failures),
        notes=tuple(notes),
        case_count=len(selected),
        fit_productive=_count(selected, "fit", PRODUCTIVE),
        fit_non_productive=_count(selected, "fit", NON_PRODUCTIVE),
        holdout_productive=_count(selected, "holdout", PRODUCTIVE),
        holdout_non_productive=_count(selected, "holdout", NON_PRODUCTIVE),
    )


def _provenance(cases: tuple[CaseSpec, ...]) -> list[str]:
    failures = []
    if not cases:
        return ["The benchmark history is empty."]
    for case in cases:
        judgment = judge(case.finding)
        if judgment.outcome not in {PRODUCTIVE, NON_PRODUCTIVE}:
            failures.append(f"{case.case_id} has an unknown outcome.")
        if DISCLAIMER not in judgment.rationale:
            failures.append(f"{case.case_id} rationale has no benchmark disclaimer.")
        if judgment.assumption_id not in {"C1", "C2"}:
            failures.append(f"{case.case_id} does not cite C1 or C2.")
        if not case.finding.obtained_after_alert:
            failures.append(f"{case.case_id} uses a finding from before the alert.")
        subject = _subject(case)
        later = [payment for payment in case.payments if payment.minute_offset > subject.minute_offset]
        if not later:
            failures.append(f"{case.case_id} has no payment after the subject payment.")
    return failures


def _coverage(cases: tuple[CaseSpec, ...], notes: list[str]) -> list[str]:
    failures = []
    roles = {case.population_role for case in cases}
    if roles != {"fit", "holdout"}:
        failures.append(f"Populations are {sorted(roles)}, not fit and holdout.")
    for role in ("fit", "holdout"):
        selected = tuple(case for case in cases if case.population_role == role)
        failures.extend(_both(selected, role, "context family", lambda case: case.context_family))
        failures.extend(_both(selected, role, "customer type", lambda case: case.customer_type))
        failures.extend(_both(selected, role, "expected rule", lambda case: case.expected_rule))
        failures.extend(
            _both(selected, role, "arrangement on file", lambda case: str(case.arrangement_on_file))
        )
        failures.extend(_both(selected, role, "currency", lambda case: case.currency_code))
        families: dict[str, int] = {}
        for case in selected:
            families[case.context_family] = families.get(case.context_family, 0) + 1
        for family, count in sorted(families.items()):
            if count < 4:
                notes.append(
                    f"{role} {family} has {count} independent cases. "
                    "That is a small slice, not a population estimate."
                )
    return failures


def _separation(cases: tuple[CaseSpec, ...]) -> list[str]:
    failures = []
    fit_accounts = {case.account_reference for case in cases if case.population_role == "fit"}
    holdout_accounts = {case.account_reference for case in cases if case.population_role == "holdout"}
    if fit_accounts & holdout_accounts:
        failures.append("An account appears in both populations.")
    fit_shapes = {_shape(case) for case in cases if case.population_role == "fit"}
    holdout_shapes = {_shape(case) for case in cases if case.population_role == "holdout"}
    shared = fit_shapes & holdout_shapes
    if shared:
        failures.append("A fit history has the same direction and channel sequence as a holdout history.")
    fit_amounts = _amounts(cases, "fit")
    holdout_amounts = _amounts(cases, "holdout")
    if fit_amounts & holdout_amounts:
        failures.append("A payment amount appears in both populations.")
    return failures


def _finding_is_independent(cases: tuple[CaseSpec, ...]) -> list[str]:
    failures = []
    arranged_same: dict[str, set[str]] = {"same": set(), "different": set()}
    for case in cases:
        subject = _subject(case)
        same = (
            case.arrangement_on_file
            and case.arrangement_beneficiary == subject.counterparty
        )
        arranged_same["same" if same else "different"].add(judge(case.finding).outcome)
    if arranged_same["same"] != {PRODUCTIVE, NON_PRODUCTIVE}:
        failures.append("Paying the arranged beneficiary does not appear in both outcomes.")
    if arranged_same["different"] != {PRODUCTIVE, NON_PRODUCTIVE}:
        failures.append("A payment outside the arrangement does not appear in both outcomes.")
    return failures


def _export_boundary() -> list[str]:
    lowered = PREDICTIVE_SQL.lower()
    failures = []
    for name in _FORBIDDEN_EXPORT_TEXT:
        if name in lowered:
            failures.append(f"The predictive export query mentions {name}.")
    for name in _EXCLUDED_FEATURE_NAMES:
        if f"'{name}'" not in PREDICTIVE_SQL:
            failures.append(f"The predictive export does not exclude {name}.")
    if "amlog_label" in lowered or "amlog_scenario" in lowered:
        failures.append("The predictive export reads labels or scenario notes.")
    return failures


def _both(cases: tuple[CaseSpec, ...], role: str, name: str, key) -> list[str]:
    groups: dict[str, set[str]] = {}
    for case in cases:
        groups.setdefault(str(key(case)), set()).add(judge(case.finding).outcome)
    failures = []
    for value, outcomes in groups.items():
        if outcomes != {PRODUCTIVE, NON_PRODUCTIVE}:
            failures.append(f"{role} {name} {value} does not have both outcomes.")
    return failures


def _count(cases: tuple[CaseSpec, ...], role: str, outcome: str) -> int:
    return sum(
        1
        for case in cases
        if case.population_role == role and judge(case.finding).outcome == outcome
    )


def _subject(case: CaseSpec):
    return next(payment for payment in case.payments if payment.event_id == case.subject_event_id)


def _shape(case: CaseSpec) -> tuple[tuple[str, str], ...]:
    ordered = sorted(case.payments, key=lambda payment: (payment.minute_offset, payment.event_id))
    return tuple((payment.direction, payment.channel) for payment in ordered)


def _amounts(cases: tuple[CaseSpec, ...], role: str) -> set[Decimal]:
    return {
        payment.amount
        for case in cases
        if case.population_role == role
        for payment in case.payments
    }

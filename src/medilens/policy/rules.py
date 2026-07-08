"""Deterministic rule engine for policy-v2 clauses (docs/policy-schema.md 5, 8).

Rules are pure functions of verified inputs: a fact value the verifier already
checked against the note, request metadata, or the recommended code set. They
contain no note text and make no network calls, so every rule decision is
reproducible from the audit record.

Fail-closed semantics live here (section 8): a missing note-sourced fact
yields insufficient_documentation, and a missing history-sourced fact yields
manual_review, because no claims-history source exists in this deployment.
Neither ever yields satisfied.
"""

import datetime
from dataclasses import dataclass
from typing import Any

from medilens.policy.structure import (
    FACT_SOURCE_HISTORY,
    FACT_SOURCE_NOTE,
    FactSpec,
    RuleSpec,
)

# Clause statuses a rule can produce. Full status set in reasoning.coverage.
STATUS_SATISFIED = "satisfied"
STATUS_NOT_SATISFIED = "not_satisfied"
STATUS_INSUFFICIENT = "insufficient_documentation"
STATUS_MANUAL_REVIEW = "manual_review"

# Duration units normalized to days. Months use a documented 30-day
# approximation; policies needing exact month arithmetic should express their
# thresholds in days or weeks.
_DAYS_PER_UNIT = {"days": 1.0, "weeks": 7.0, "months": 30.0}


@dataclass(frozen=True)
class RuleOutcome:
    """The result of one deterministic rule: a status plus an auditable detail."""

    status: str
    detail: str


def _missing_fact_outcome(fact_spec: FactSpec) -> RuleOutcome:
    """Fail-closed outcome for a fact the rule needs but does not have."""
    if fact_spec.source == FACT_SOURCE_HISTORY:
        return RuleOutcome(
            status=STATUS_MANUAL_REVIEW,
            detail=(
                f"requires {fact_spec.key} from claims or procedure history; "
                "no history source is available in this deployment, so this "
                "clause defers to human review"
            ),
        )
    if fact_spec.source == FACT_SOURCE_NOTE:
        return RuleOutcome(
            status=STATUS_INSUFFICIENT,
            detail=(
                f"the note does not document {fact_spec.key}; absence of "
                "evidence is never satisfaction"
            ),
        )
    # request-sourced values are always present; reaching here is a policy
    # authoring error surfaced loudly.
    raise ValueError(
        f"fact {fact_spec.key!r} with source {fact_spec.source!r} was missing "
        "at rule evaluation; request-sourced facts must always be supplied"
    )


def _to_days(value: float, unit: str | None) -> float:
    if unit is None:
        raise ValueError("duration comparison requires a unit")
    if unit not in _DAYS_PER_UNIT:
        raise ValueError(f"unknown duration unit {unit!r}")
    return value * _DAYS_PER_UNIT[unit]


def _compare_duration(
    rule: RuleSpec,
    fact_spec: FactSpec,
    value: float,
    minimum: bool,
) -> RuleOutcome:
    threshold = float(rule.params["minimum" if minimum else "maximum"])
    rule_unit = rule.params.get("unit", fact_spec.unit)
    value_days = _to_days(value, fact_spec.unit)
    threshold_days = _to_days(threshold, rule_unit)

    if minimum:
        passed = value_days >= threshold_days
        comparator = ">=" if passed else "<"
    else:
        passed = value_days <= threshold_days
        comparator = "<=" if passed else ">"
    detail = (
        f"rule {rule.op}: documented {value:g} {fact_spec.unit} {comparator} "
        f"threshold {threshold:g} {rule_unit}"
    )
    status = STATUS_SATISFIED if passed else STATUS_NOT_SATISFIED
    return RuleOutcome(status=status, detail=detail)


def _compare_count(
    rule: RuleSpec, fact_spec: FactSpec, value: float, minimum: bool
) -> RuleOutcome:
    threshold = float(rule.params["minimum" if minimum else "maximum"])
    if minimum:
        passed = value >= threshold
        comparator = ">=" if passed else "<"
    else:
        passed = value <= threshold
        comparator = "<=" if passed else ">"
    detail = (
        f"rule {rule.op}: documented {value:g} {comparator} threshold "
        f"{threshold:g}"
    )
    status = STATUS_SATISFIED if passed else STATUS_NOT_SATISFIED
    return RuleOutcome(status=status, detail=detail)


def evaluate_rule(
    rule: RuleSpec,
    fact_specs: dict[str, FactSpec],
    fact_values: dict[str, Any],
    date_of_service: datetime.date,
    recommended_codes: frozenset[str],
) -> RuleOutcome:
    """Evaluate one deterministic rule against verified inputs.

    fact_values maps fact key to its verified, typed value (float for duration
    and count, bool for boolean, datetime.date for date). A key absent from
    fact_values means the fact was not documented or not verifiable, which
    triggers the fail-closed path for its declared source.
    """
    if rule.op == "code_in_set":
        allowed = frozenset(rule.params.get("allowed", []))
        if len(recommended_codes) == 0:
            return RuleOutcome(
                status=STATUS_INSUFFICIENT,
                detail=(
                    "rule code_in_set: no documentation-supported code was "
                    "recommended, so code membership cannot be established"
                ),
            )
        matched = recommended_codes & allowed
        if len(matched) > 0:
            matched_text = ", ".join(sorted(matched))
            return RuleOutcome(
                status=STATUS_SATISFIED,
                detail=f"rule code_in_set: {matched_text} in allowed set",
            )
        codes_text = ", ".join(sorted(recommended_codes))
        return RuleOutcome(
            status=STATUS_NOT_SATISFIED,
            detail=f"rule code_in_set: none of [{codes_text}] in allowed set",
        )

    fact_key = rule.params["fact"]
    fact_spec = fact_specs[fact_key]
    if fact_key not in fact_values:
        return _missing_fact_outcome(fact_spec)
    value = fact_values[fact_key]

    if rule.op == "min_duration":
        return _compare_duration(rule, fact_spec, float(value), minimum=True)
    if rule.op == "max_duration":
        return _compare_duration(rule, fact_spec, float(value), minimum=False)
    if rule.op == "min_count":
        return _compare_count(rule, fact_spec, float(value), minimum=True)
    if rule.op == "max_count":
        return _compare_count(rule, fact_spec, float(value), minimum=False)

    if rule.op == "frequency_limit":
        maximum = float(rule.params["maximum"])
        window_months = rule.params.get("window_months")
        count = float(value)
        passed = count <= maximum
        window_text = (
            f" within {window_months} months" if window_months is not None else ""
        )
        comparator = "<=" if passed else ">"
        detail = (
            f"rule frequency_limit: documented count {count:g} {comparator} "
            f"maximum {maximum:g}{window_text}"
        )
        status = STATUS_SATISFIED if passed else STATUS_NOT_SATISFIED
        return RuleOutcome(status=status, detail=detail)

    if rule.op == "date_within":
        fact_date: datetime.date = value
        delta_days = (date_of_service - fact_date).days
        min_days = rule.params.get("min_days")
        max_days = rule.params.get("max_days")
        passed = True
        if min_days is not None and delta_days < int(min_days):
            passed = False
        if max_days is not None and delta_days > int(max_days):
            passed = False
        detail = (
            f"rule date_within: {fact_key} is {delta_days} days before the "
            f"date of service (bounds min {min_days}, max {max_days})"
        )
        status = STATUS_SATISFIED if passed else STATUS_NOT_SATISFIED
        return RuleOutcome(status=status, detail=detail)

    if rule.op == "boolean_true" or rule.op == "boolean_false":
        expected = rule.op == "boolean_true"
        actual = bool(value)
        passed = actual == expected
        detail = f"rule {rule.op}: documented value is {str(actual).lower()}"
        status = STATUS_SATISFIED if passed else STATUS_NOT_SATISFIED
        return RuleOutcome(status=status, detail=detail)

    # parse_policy_structure validates operators, so this is unreachable
    # unless a new operator is added without an implementation.
    raise ValueError(f"rule operator {rule.op!r} has no implementation")

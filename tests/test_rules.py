"""Tests for the deterministic rule engine (policy schema v2).

Rules are pure functions of verified inputs, so these tests need no database,
model, or note text. The fail-closed paths are the most important cases here:
a missing note fact is insufficient documentation, a missing history fact is
manual review, and neither is ever satisfied.
"""

import datetime

import pytest

from medilens.policy.rules import (
    STATUS_INSUFFICIENT,
    STATUS_MANUAL_REVIEW,
    STATUS_NOT_SATISFIED,
    STATUS_SATISFIED,
    FactValue,
    evaluate_rule,
    normalize_duration_unit,
)
from medilens.policy.structure import FactSpec, RuleSpec

DOS = datetime.date(2026, 6, 1)
NO_CODES: frozenset[str] = frozenset()

NOTE_DURATION = FactSpec(
    key="symptom_duration", type="duration", source="note", unit="weeks"
)
NOTE_COUNT = FactSpec(key="relief_percent", type="count", source="note", unit="percent")
HISTORY_COUNT = FactSpec(key="rfa_12mo", type="count", source="history")
NOTE_BOOLEAN = FactSpec(key="infection_present", type="boolean", source="note")
NOTE_DATE = FactSpec(key="block_date", type="date", source="note")

SPECS = {
    "symptom_duration": NOTE_DURATION,
    "relief_percent": NOTE_COUNT,
    "rfa_12mo": HISTORY_COUNT,
    "infection_present": NOTE_BOOLEAN,
    "block_date": NOTE_DATE,
}


def _run(rule: RuleSpec, facts: dict, codes: frozenset[str] = NO_CODES):
    return evaluate_rule(rule, SPECS, facts, DOS, codes)


# --- fail-closed missing-data behavior ---------------------------------------


def test_missing_note_fact_is_insufficient_documentation() -> None:
    rule = RuleSpec(op="min_duration", params={"fact": "symptom_duration", "minimum": 6, "unit": "weeks"})

    outcome = _run(rule, {})

    assert outcome.status == STATUS_INSUFFICIENT
    assert "does not document" in outcome.detail


def test_missing_history_fact_is_manual_review() -> None:
    rule = RuleSpec(op="frequency_limit", params={"fact": "rfa_12mo", "maximum": 2, "window_months": 12})

    outcome = _run(rule, {})

    assert outcome.status == STATUS_MANUAL_REVIEW
    assert "history" in outcome.detail


# --- duration comparison and unit conversion (owned by code) -----------------


def test_min_duration_satisfied_same_unit() -> None:
    rule = RuleSpec(op="min_duration", params={"fact": "symptom_duration", "minimum": 6, "unit": "weeks"})

    outcome = _run(rule, {"symptom_duration": FactValue(value=8.0, unit="weeks")})

    assert outcome.status == STATUS_SATISFIED
    assert "8 weeks" in outcome.detail


def test_min_duration_converts_documented_months_in_code() -> None:
    # The note documented "2 months"; the threshold is 6 weeks. Code converts
    # (60 days >= 42 days); the model never pre-normalizes.
    rule = RuleSpec(op="min_duration", params={"fact": "symptom_duration", "minimum": 6, "unit": "weeks"})

    outcome = _run(rule, {"symptom_duration": FactValue(value=2.0, unit="months")})

    assert outcome.status == STATUS_SATISFIED
    assert "2 months" in outcome.detail


def test_min_duration_not_satisfied_after_conversion() -> None:
    # 30 days < 6 weeks (42 days).
    rule = RuleSpec(op="min_duration", params={"fact": "symptom_duration", "minimum": 6, "unit": "weeks"})

    outcome = _run(rule, {"symptom_duration": FactValue(value=30.0, unit="days")})

    assert outcome.status == STATUS_NOT_SATISFIED


def test_normalize_duration_unit_aliases() -> None:
    assert normalize_duration_unit("Weeks") == "weeks"
    assert normalize_duration_unit("week") == "weeks"
    assert normalize_duration_unit("mo") == "months"
    assert normalize_duration_unit("day") == "days"
    assert normalize_duration_unit("fortnights") is None
    assert normalize_duration_unit(None) is None
    assert normalize_duration_unit("") is None


# --- count, frequency, boolean, date, code_in_set -----------------------------


def test_min_count_thresholds() -> None:
    rule = RuleSpec(op="min_count", params={"fact": "relief_percent", "minimum": 50})

    passing = _run(rule, {"relief_percent": FactValue(value=80.0, unit="percent")})
    failing = _run(rule, {"relief_percent": FactValue(value=40.0, unit="percent")})

    assert passing.status == STATUS_SATISFIED
    assert failing.status == STATUS_NOT_SATISFIED


def test_frequency_limit_with_value() -> None:
    rule = RuleSpec(op="frequency_limit", params={"fact": "rfa_12mo", "maximum": 2, "window_months": 12})

    under = _run(rule, {"rfa_12mo": FactValue(value=1.0, unit=None)})
    over = _run(rule, {"rfa_12mo": FactValue(value=3.0, unit=None)})

    assert under.status == STATUS_SATISFIED
    assert over.status == STATUS_NOT_SATISFIED


def test_boolean_rules() -> None:
    true_rule = RuleSpec(op="boolean_true", params={"fact": "infection_present"})
    false_rule = RuleSpec(op="boolean_false", params={"fact": "infection_present"})
    facts = {"infection_present": FactValue(value=False, unit=None)}

    assert _run(true_rule, facts).status == STATUS_NOT_SATISFIED
    assert _run(false_rule, facts).status == STATUS_SATISFIED


def test_date_within_bounds() -> None:
    rule = RuleSpec(op="date_within", params={"fact": "block_date", "min_days": 0, "max_days": 90})
    recent = {"block_date": FactValue(value=datetime.date(2026, 5, 1), unit=None)}
    stale = {"block_date": FactValue(value=datetime.date(2025, 6, 1), unit=None)}

    assert _run(rule, recent).status == STATUS_SATISFIED
    assert _run(rule, stale).status == STATUS_NOT_SATISFIED


def test_code_in_set() -> None:
    rule = RuleSpec(op="code_in_set", params={"allowed": ["M54.16", "M51.16"]})

    hit = _run(rule, {}, codes=frozenset({"M54.16"}))
    miss = _run(rule, {}, codes=frozenset({"S99.999"}))
    none = _run(rule, {}, codes=frozenset())

    assert hit.status == STATUS_SATISFIED
    assert miss.status == STATUS_NOT_SATISFIED
    assert none.status == STATUS_INSUFFICIENT


def test_unknown_duration_unit_raises_loudly() -> None:
    # The verifier drops unconvertible units before rules run; if one reaches
    # the engine anyway, it fails loudly rather than guessing.
    rule = RuleSpec(op="min_duration", params={"fact": "symptom_duration", "minimum": 6, "unit": "weeks"})

    with pytest.raises(ValueError):
        _run(rule, {"symptom_duration": FactValue(value=2.0, unit="fortnights")})

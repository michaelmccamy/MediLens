"""Tests for the clause evaluator and the computed coverage determination.

These pin the fail-closed semantics of policy schema v2: silence never
passes, missing history defers to a human, the bypass override is computed in
code from a verified-satisfied trigger, and the determination and score are
pure functions of clause statuses.
"""

import datetime

import pytest

from medilens.policy.structure import (
    ClauseSpec,
    FactSpec,
    JudgmentSpec,
    PolicyStructure,
    RuleSpec,
)
from medilens.reasoning.coverage import (
    DETERMINATION_DOES_NOT_MEET,
    DETERMINATION_INSUFFICIENT,
    DETERMINATION_MANUAL_REVIEW,
    DETERMINATION_MEETS,
    SCORE_DOES_NOT_MEET,
    SCORE_MEETS,
    combine_assessments,
    evaluate_policy_coverage,
)
from medilens.reasoning.verification import (
    LocatedSpan,
    VerifiedClauseJudgment,
    VerifiedClinicalFact,
)

DOS = datetime.date(2026, 6, 1)
PID = "SYN-TEST-001"
SPAN = LocatedSpan(text="evidence", start_offset=0, end_offset=8)


class _Row:
    policy_identifier = PID


def _structure(clauses: list[ClauseSpec], facts: list[FactSpec] | None = None) -> PolicyStructure:
    return PolicyStructure(
        schema_version="policy-v2",
        version=1,
        source_type="synthetic",
        source_authoritative=False,
        source_citation="test",
        required_facts=tuple(facts or []),
        clauses=tuple(clauses),
    )


def _judged(clause_id: str, required: bool = True, bypasses: tuple = ()) -> ClauseSpec:
    return ClauseSpec(
        clause_id=clause_id,
        title=clause_id,
        text="text",
        evaluation="model_judged",
        required=required,
        bypasses=bypasses,
        judgment=JudgmentSpec(question="?"),
    )


def _judgment(clause_id: str, status: str, with_evidence: bool = True) -> VerifiedClauseJudgment:
    evidence = (SPAN,) if with_evidence else ()
    return VerifiedClauseJudgment(
        policy_identifier=PID, clause_id=clause_id, status=status, evidence=evidence
    )


def _evaluate(structure, facts=None, judgments=None, codes=frozenset()):
    return evaluate_policy_coverage(
        _Row(), structure, facts or {}, judgments or {}, DOS, codes
    )


# --- silence fails closed -----------------------------------------------------


def test_missing_judgment_is_insufficient() -> None:
    structure = _structure([_judged("a")])

    assessment = _evaluate(structure)

    assert assessment.clause_results[0].status == "insufficient_documentation"
    assert assessment.determination == DETERMINATION_INSUFFICIENT


def test_manual_review_clause_always_defers() -> None:
    clause = ClauseSpec(
        clause_id="lookback", title="t", text="t",
        evaluation="manual_review", required=True,
    )
    structure = _structure([clause])

    assessment = _evaluate(structure)

    assert assessment.clause_results[0].status == "manual_review"
    assert assessment.clause_results[0].decided_by == "deferred"
    assert assessment.determination == DETERMINATION_MANUAL_REVIEW


# --- hybrid combination --------------------------------------------------------


def _hybrid_structure() -> PolicyStructure:
    fact = FactSpec(key="duration", type="duration", source="note", unit="weeks")
    clause = ClauseSpec(
        clause_id="h", title="t", text="t", evaluation="hybrid", required=True,
        rule=RuleSpec(op="min_duration", params={"fact": "duration", "minimum": 6, "unit": "weeks"}),
        judgment=JudgmentSpec(question="?"),
    )
    return _structure([clause], facts=[fact])


def _duration_fact(value: float) -> dict:
    return {
        "duration": VerifiedClinicalFact(
            key="duration", value=value, unit="weeks", evidence=SPAN
        )
    }


def test_hybrid_both_pass() -> None:
    structure = _hybrid_structure()
    judgments = {(PID, "h"): _judgment("h", "satisfied")}

    assessment = _evaluate(structure, _duration_fact(8.0), judgments)

    result = assessment.clause_results[0]
    assert result.status == "satisfied"
    assert result.decided_by == "rule+model"
    assert assessment.determination == DETERMINATION_MEETS


def test_hybrid_rule_failure_wins_over_judgment_pass() -> None:
    structure = _hybrid_structure()
    judgments = {(PID, "h"): _judgment("h", "satisfied")}

    assessment = _evaluate(structure, _duration_fact(3.0), judgments)

    assert assessment.clause_results[0].status == "not_satisfied"
    assert assessment.determination == DETERMINATION_DOES_NOT_MEET


def test_hybrid_missing_fact_is_insufficient_even_if_judged_satisfied() -> None:
    structure = _hybrid_structure()
    judgments = {(PID, "h"): _judgment("h", "satisfied")}

    assessment = _evaluate(structure, {}, judgments)

    assert assessment.clause_results[0].status == "insufficient_documentation"


def test_hybrid_judgment_failure_wins_over_rule_pass() -> None:
    structure = _hybrid_structure()
    judgments = {(PID, "h"): _judgment("h", "not_satisfied")}

    assessment = _evaluate(structure, _duration_fact(8.0), judgments)

    assert assessment.clause_results[0].status == "not_satisfied"


# --- bypass override (policy-level, computed in code) --------------------------


def _override_structure() -> PolicyStructure:
    lookback = ClauseSpec(
        clause_id="lookback", title="t", text="t",
        evaluation="manual_review", required=True,
    )
    gate_a = _judged("gate_a")
    gate_b = _judged("gate_b")
    trigger = _judged(
        "red_flag", required=False,
        bypasses=("gate_a", "gate_b", "lookback"),
    )
    return _structure([gate_a, gate_b, trigger, lookback])


def test_satisfied_override_bypasses_entire_declared_set() -> None:
    structure = _override_structure()
    judgments = {(PID, "red_flag"): _judgment("red_flag", "satisfied")}

    assessment = _evaluate(structure, judgments=judgments)

    by_id = {r.clause_id: r for r in assessment.clause_results}
    assert by_id["gate_a"].status == "not_applicable"
    assert by_id["gate_b"].status == "not_applicable"
    # The manual_review lookback is explicitly listed, so the emergency moots
    # it too (not_applicable is moot, not passed).
    assert by_id["lookback"].status == "not_applicable"
    assert by_id["gate_a"].decided_by == "override"
    assert assessment.determination == DETERMINATION_MEETS


def test_unsatisfied_override_bypasses_nothing() -> None:
    structure = _override_structure()
    judgments = {
        (PID, "red_flag"): _judgment("red_flag", "insufficient_documentation", with_evidence=False),
        (PID, "gate_a"): _judgment("gate_a", "satisfied"),
        (PID, "gate_b"): _judgment("gate_b", "satisfied"),
    }

    assessment = _evaluate(structure, judgments=judgments)

    by_id = {r.clause_id: r for r in assessment.clause_results}
    assert by_id["gate_a"].status == "satisfied"
    assert by_id["lookback"].status == "manual_review"
    assert assessment.determination == DETERMINATION_MANUAL_REVIEW


def test_bypass_only_covers_listed_clauses() -> None:
    unlisted = _judged("unlisted")
    trigger = _judged("red_flag", required=False, bypasses=("unlisted",))
    other = _judged("other")
    structure = _structure([unlisted, other, trigger])
    judgments = {(PID, "red_flag"): _judgment("red_flag", "satisfied")}

    assessment = _evaluate(structure, judgments=judgments)

    by_id = {r.clause_id: r for r in assessment.clause_results}
    assert by_id["unlisted"].status == "not_applicable"
    # "other" is not in the bypass list, so it still fails closed.
    assert by_id["other"].status == "insufficient_documentation"


# --- determination precedence and computed score --------------------------------


def test_hard_failure_outranks_manual_review() -> None:
    lookback = ClauseSpec(
        clause_id="lookback", title="t", text="t",
        evaluation="manual_review", required=True,
    )
    failing = _judged("failing")
    structure = _structure([failing, lookback])
    judgments = {(PID, "failing"): _judgment("failing", "not_satisfied")}

    assessment = _evaluate(structure, judgments=judgments)

    assert assessment.determination == DETERMINATION_DOES_NOT_MEET
    assert assessment.denial_risk_score == SCORE_DOES_NOT_MEET


def test_contradictory_produces_does_not_meet() -> None:
    structure = _structure([_judged("a")])
    judgments = {(PID, "a"): _judgment("a", "contradictory_documentation")}

    assessment = _evaluate(structure, judgments=judgments)

    assert assessment.determination == DETERMINATION_DOES_NOT_MEET


def test_meets_score_and_rationale() -> None:
    structure = _structure([_judged("a")])
    judgments = {(PID, "a"): _judgment("a", "satisfied")}

    assessment = _evaluate(structure, judgments=judgments)

    assert assessment.determination == DETERMINATION_MEETS
    assert assessment.denial_risk_score == SCORE_MEETS
    assert "a=satisfied" in assessment.determination_rationale


def test_insufficient_score_scales_with_failing_fraction() -> None:
    clauses = [_judged("a"), _judged("b"), _judged("c"), _judged("d")]
    structure = _structure(clauses)
    # One of four insufficient (others satisfied).
    judgments_one = {
        (PID, "a"): _judgment("a", "satisfied"),
        (PID, "b"): _judgment("b", "satisfied"),
        (PID, "c"): _judgment("c", "satisfied"),
    }
    one = _evaluate(structure, judgments=judgments_one)
    # All four insufficient.
    all_missing = _evaluate(structure, judgments={})

    assert one.determination == DETERMINATION_INSUFFICIENT
    assert all_missing.determination == DETERMINATION_INSUFFICIENT
    assert one.denial_risk_score < all_missing.denial_risk_score
    assert one.denial_risk_score == pytest.approx(0.35 + 0.30 * (1 / 4))
    assert all_missing.denial_risk_score == pytest.approx(0.65)


def test_optional_clause_does_not_drive_determination() -> None:
    optional = _judged("optional", required=False)
    required = _judged("required")
    structure = _structure([optional, required])
    judgments = {(PID, "required"): _judgment("required", "satisfied")}

    assessment = _evaluate(structure, judgments=judgments)

    # The optional clause is insufficient (no judgment) but the determination
    # only weighs required clauses.
    assert assessment.determination == DETERMINATION_MEETS


def test_combine_assessments_worst_governs() -> None:
    meets_structure = _structure([_judged("a")])
    meets = _evaluate(meets_structure, judgments={(PID, "a"): _judgment("a", "satisfied")})
    insufficient = _evaluate(meets_structure, judgments={})

    combined = combine_assessments([meets, insufficient])

    assert combined.determination == DETERMINATION_INSUFFICIENT
    assert len(combined.clause_results) == 2

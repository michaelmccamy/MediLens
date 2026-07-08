"""Clause evaluation and the computed coverage determination (policy-v2).

This is where policy satisfaction is decided, in code, from verified inputs:
the rule engine for deterministic clauses, verified model judgments for
qualitative ones, both for hybrids, and a hard defer for manual-review
clauses. Overrides are applied here (never by the model), the overall
determination is computed by fixed precedence, and the denial-risk score is
derived from clause statuses so the number is auditable: the audit record can
say exactly which clauses produced it.

Fail-closed invariants (docs/policy-schema.md sections 7, 8, 10):
- Only satisfied and not_applicable count as met.
- A judgment-bearing clause with no verified judgment is
  insufficient_documentation. Silence never passes.
- A bypass override only fires when its trigger clause itself resolved
  satisfied with verified evidence, and only for the clauses the policy
  explicitly lists. The model can never trigger one.
- manual_review is sticky against everything except an explicit policy bypass:
  it can never be upgraded to satisfied, and it forces the overall
  determination to manual_review unless a hard failure outranks it or a
  policy-declared override renders the clause not_applicable (moot, not
  passed).
"""

import datetime
from dataclasses import dataclass

from medilens.db.models import PayerPolicy
from medilens.policy.rules import FactValue, evaluate_rule
from medilens.policy.structure import (
    EVALUATION_DETERMINISTIC,
    EVALUATION_HYBRID,
    EVALUATION_MANUAL_REVIEW,
    EVALUATION_MODEL_JUDGED,
    ClauseSpec,
    PolicyStructure,
)
from medilens.reasoning.verification import (
    LocatedSpan,
    VerifiedClauseJudgment,
    VerifiedClinicalFact,
)

# Clause statuses (docs/policy-schema.md section 7).
STATUS_SATISFIED = "satisfied"
STATUS_NOT_SATISFIED = "not_satisfied"
STATUS_INSUFFICIENT = "insufficient_documentation"
STATUS_CONTRADICTORY = "contradictory_documentation"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_MANUAL_REVIEW = "manual_review"

CLAUSE_STATUSES = frozenset(
    {
        STATUS_SATISFIED,
        STATUS_NOT_SATISFIED,
        STATUS_INSUFFICIENT,
        STATUS_CONTRADICTORY,
        STATUS_NOT_APPLICABLE,
        STATUS_MANUAL_REVIEW,
    }
)

# Overall determinations (section 7), in precedence order: an earlier value
# outranks a later one when combining clauses or policies.
DETERMINATION_DOES_NOT_MEET = "does_not_meet"
DETERMINATION_MANUAL_REVIEW = "manual_review"
DETERMINATION_INSUFFICIENT = "insufficient_documentation"
DETERMINATION_MEETS = "meets_criteria"

DETERMINATIONS = frozenset(
    {
        DETERMINATION_DOES_NOT_MEET,
        DETERMINATION_MANUAL_REVIEW,
        DETERMINATION_INSUFFICIENT,
        DETERMINATION_MEETS,
    }
)

_DETERMINATION_PRECEDENCE = [
    DETERMINATION_DOES_NOT_MEET,
    DETERMINATION_MANUAL_REVIEW,
    DETERMINATION_INSUFFICIENT,
    DETERMINATION_MEETS,
]

# Computed denial-risk constants (section 11, decision 1). The score is a
# function of the determination and the failing-clause fraction, so every
# number is reconstructable from the clause statuses in the audit record.
# These constants are the tuning surface for the eval threshold sweep.
SCORE_MEETS = 0.15
SCORE_DOES_NOT_MEET = 0.85
SCORE_MANUAL_REVIEW = 0.50
_SCORE_INSUFFICIENT_BASE = 0.35
_SCORE_INSUFFICIENT_SPAN = 0.30


@dataclass(frozen=True)
class ClauseResult:
    """One clause's evaluated status, with the auditable reason."""

    policy_identifier: str
    clause_id: str
    title: str
    status: str
    decided_by: str  # rule | model | rule+model | override | deferred
    detail: str
    evidence: tuple[LocatedSpan, ...]
    required: bool


@dataclass(frozen=True)
class CoverageAssessment:
    """The computed coverage outcome for one request."""

    clause_results: list[ClauseResult]
    determination: str
    denial_risk_score: float
    determination_rationale: str


def _judgment_result(
    clause: ClauseSpec,
    policy_identifier: str,
    judgment: VerifiedClauseJudgment | None,
) -> tuple[str, str, tuple[LocatedSpan, ...]]:
    """Resolve the model-judgment half of a clause: (status, detail, evidence).

    A missing judgment fails closed: the model did not assess the clause, so
    the note is treated as not documenting it.
    """
    if judgment is None:
        return (
            STATUS_INSUFFICIENT,
            "no verified model judgment for this clause; silence fails closed",
            (),
        )
    evidence_count = len(judgment.evidence)
    detail = f"model judgment: {judgment.status} ({evidence_count} evidence span(s))"
    return judgment.status, detail, judgment.evidence


def _evaluate_clause_raw(
    clause: ClauseSpec,
    policy_identifier: str,
    structure: PolicyStructure,
    fact_values: dict[str, FactValue],
    judgments: dict[tuple[str, str], VerifiedClauseJudgment],
    date_of_service: datetime.date,
    recommended_codes: frozenset[str],
) -> ClauseResult:
    """Evaluate one clause before overrides are applied."""
    if clause.evaluation == EVALUATION_MANUAL_REVIEW:
        return ClauseResult(
            policy_identifier=policy_identifier,
            clause_id=clause.clause_id,
            title=clause.title,
            status=STATUS_MANUAL_REVIEW,
            decided_by="deferred",
            detail="this clause always defers to human review",
            evidence=(),
            required=clause.required,
        )

    rule_status: str | None = None
    rule_detail = ""
    if clause.needs_rule:
        outcome = evaluate_rule(
            clause.rule,
            structure.fact_specs_by_key(),
            fact_values,
            date_of_service,
            recommended_codes,
        )
        rule_status = outcome.status
        rule_detail = outcome.detail

    judgment_status: str | None = None
    judgment_detail = ""
    judgment_evidence: tuple[LocatedSpan, ...] = ()
    if clause.needs_judgment:
        judgment = judgments.get((policy_identifier, clause.clause_id))
        judgment_status, judgment_detail, judgment_evidence = _judgment_result(
            clause, policy_identifier, judgment
        )

    if clause.evaluation == EVALUATION_DETERMINISTIC:
        return ClauseResult(
            policy_identifier=policy_identifier,
            clause_id=clause.clause_id,
            title=clause.title,
            status=rule_status,
            decided_by="rule",
            detail=rule_detail,
            evidence=(),
            required=clause.required,
        )

    if clause.evaluation == EVALUATION_MODEL_JUDGED:
        return ClauseResult(
            policy_identifier=policy_identifier,
            clause_id=clause.clause_id,
            title=clause.title,
            status=judgment_status,
            decided_by="model",
            detail=judgment_detail,
            evidence=judgment_evidence,
            required=clause.required,
        )

    # Hybrid: both halves must pass; failures combine by severity, rule first
    # (docs/policy-schema.md section 8).
    combined_detail = f"{rule_detail}; {judgment_detail}"
    hard_failures = (STATUS_NOT_SATISFIED, STATUS_CONTRADICTORY)
    soft_failures = (STATUS_INSUFFICIENT, STATUS_MANUAL_REVIEW)
    combined_status = STATUS_SATISFIED
    for half_status in (rule_status, judgment_status):
        if half_status in hard_failures:
            combined_status = half_status
            break
    else:
        for half_status in (rule_status, judgment_status):
            if half_status in soft_failures:
                combined_status = half_status
                break
    return ClauseResult(
        policy_identifier=policy_identifier,
        clause_id=clause.clause_id,
        title=clause.title,
        status=combined_status,
        decided_by="rule+model",
        detail=combined_detail,
        evidence=judgment_evidence,
        required=clause.required,
    )


def _apply_overrides(
    structure: PolicyStructure, results: list[ClauseResult]
) -> list[ClauseResult]:
    """Apply policy-level bypass overrides, computed in code.

    A clause whose spec declares `bypasses` is an override trigger: when it
    resolves satisfied with verified evidence, every clause it explicitly
    lists becomes not_applicable. This is the "image now, skip the gating
    criteria" semantic: a verified red flag moots the entire prerequisite set
    the policy author listed, which may include a manual_review clause such as
    an imaging-recency lookback (the emergency makes the lookback moot; this
    renders it not applicable, which is different from asserting it passed).

    The model can never trigger a bypass: only an evaluated, verified
    satisfied status on the trigger clause fires it, and membership comes
    from policy data, never from engine special cases.
    """
    status_by_id: dict[str, str] = {}
    for result in results:
        status_by_id[result.clause_id] = result.status

    # Collect the clauses mooted by each satisfied override trigger.
    bypassed_by: dict[str, str] = {}
    for clause in structure.clauses:
        if len(clause.bypasses) == 0:
            continue
        if status_by_id.get(clause.clause_id) != STATUS_SATISFIED:
            continue
        for bypassed_id in clause.bypasses:
            if bypassed_id not in bypassed_by:
                bypassed_by[bypassed_id] = clause.clause_id

    adjusted: list[ClauseResult] = []
    for result in results:
        trigger_id = bypassed_by.get(result.clause_id)
        if trigger_id is not None:
            adjusted.append(
                ClauseResult(
                    policy_identifier=result.policy_identifier,
                    clause_id=result.clause_id,
                    title=result.title,
                    status=STATUS_NOT_APPLICABLE,
                    decided_by="override",
                    detail=(
                        f"not applicable: override clause {trigger_id} is "
                        "satisfied with verified evidence and bypasses this "
                        "clause"
                    ),
                    evidence=result.evidence,
                    required=result.required,
                )
            )
        else:
            adjusted.append(result)
    return adjusted


def _determine(results: list[ClauseResult]) -> str:
    """Compute the overall determination by fixed precedence (section 7)."""
    required = [result for result in results if result.required]
    for result in required:
        if result.status in (STATUS_NOT_SATISFIED, STATUS_CONTRADICTORY):
            return DETERMINATION_DOES_NOT_MEET
    for result in required:
        if result.status == STATUS_MANUAL_REVIEW:
            return DETERMINATION_MANUAL_REVIEW
    for result in required:
        if result.status == STATUS_INSUFFICIENT:
            return DETERMINATION_INSUFFICIENT
    return DETERMINATION_MEETS


def _score(determination: str, results: list[ClauseResult]) -> float:
    """Derive the denial-risk score from the determination and clause statuses.

    Auditable by construction: the audit record's clause statuses reproduce
    this number exactly. For insufficient documentation the score scales with
    the fraction of required clauses that are insufficient, so a note missing
    one element scores lower than a note missing everything. manual_review is
    a flat placeholder; it is surfaced as needs-human-review and excluded from
    denial-prediction metrics (decision 4), never treated as a prediction.
    """
    if determination == DETERMINATION_MEETS:
        return SCORE_MEETS
    if determination == DETERMINATION_DOES_NOT_MEET:
        return SCORE_DOES_NOT_MEET
    if determination == DETERMINATION_MANUAL_REVIEW:
        return SCORE_MANUAL_REVIEW

    required = [result for result in results if result.required]
    if len(required) == 0:
        return _SCORE_INSUFFICIENT_BASE
    insufficient = [
        result for result in required if result.status == STATUS_INSUFFICIENT
    ]
    fraction = len(insufficient) / len(required)
    return round(_SCORE_INSUFFICIENT_BASE + _SCORE_INSUFFICIENT_SPAN * fraction, 4)


def _rationale(determination: str, results: list[ClauseResult]) -> str:
    """Render the computed, auditable explanation of the determination."""
    parts: list[str] = []
    for result in results:
        parts.append(f"{result.clause_id}={result.status}")
    statuses_text = "; ".join(parts)
    return (
        f"Computed from clause statuses ({statuses_text}). "
        f"Determination: {determination}."
    )


def evaluate_policy_coverage(
    policy_row: PayerPolicy,
    structure: PolicyStructure,
    clinical_facts: dict[str, VerifiedClinicalFact],
    clause_judgments: dict[tuple[str, str], VerifiedClauseJudgment],
    date_of_service: datetime.date,
    recommended_codes: frozenset[str],
) -> CoverageAssessment:
    """Evaluate every clause of one policy and compute its determination."""
    fact_values: dict[str, FactValue] = {}
    for key, fact in clinical_facts.items():
        fact_values[key] = FactValue(value=fact.value, unit=fact.unit)

    raw_results: list[ClauseResult] = []
    for clause in structure.clauses:
        raw_results.append(
            _evaluate_clause_raw(
                clause,
                policy_row.policy_identifier,
                structure,
                fact_values,
                clause_judgments,
                date_of_service,
                recommended_codes,
            )
        )

    results = _apply_overrides(structure, raw_results)
    determination = _determine(results)
    score = _score(determination, results)
    rationale = _rationale(determination, results)
    return CoverageAssessment(
        clause_results=results,
        determination=determination,
        denial_risk_score=score,
        determination_rationale=rationale,
    )


def combine_assessments(assessments: list[CoverageAssessment]) -> CoverageAssessment:
    """Combine per-policy assessments into one overall assessment.

    With the beachhead seeds a request matches one policy, but when several
    match, the worst determination by precedence governs (fail closed), its
    score is used, and all clause results and rationales are preserved.
    """
    if len(assessments) == 1:
        return assessments[0]

    all_results: list[ClauseResult] = []
    rationales: list[str] = []
    for assessment in assessments:
        all_results.extend(assessment.clause_results)
        rationales.append(assessment.determination_rationale)

    governing = assessments[0]
    for assessment in assessments[1:]:
        current_rank = _DETERMINATION_PRECEDENCE.index(governing.determination)
        candidate_rank = _DETERMINATION_PRECEDENCE.index(assessment.determination)
        if candidate_rank < current_rank:
            governing = assessment

    return CoverageAssessment(
        clause_results=all_results,
        determination=governing.determination,
        denial_risk_score=governing.denial_risk_score,
        determination_rationale=" | ".join(rationales),
    )

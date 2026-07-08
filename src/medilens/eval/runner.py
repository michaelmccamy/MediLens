"""Run labeled cases through the pipeline and assemble an evaluation report.

The runner reads (retrieval) but never writes: evaluation measures the
pipeline, it does not create audit records. Each case runs through the same
run_validation the CLI and UI use, so the metrics reflect real behavior.

Refusals (PHI, no applicable policy, missing retrieval) are first-class
outcomes here, not errors to swallow: a case is scored, refused, or errored,
and the report counts each so a refusal never silently inflates or deflates a
metric.

Under policy schema v2 the runner also scores the computed layer: the overall
determination against its gold label, and each intentionally-targeted clause
status against its expected status. Cases whose determination is
manual_review are EXCLUDED from denial precision/recall (decision 4,
docs/policy-schema.md): "needs a human" is not a denial prediction, and
counting it either way would distort the numbers.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from medilens.eval.dataset import EvalCase
from medilens.eval.metrics import (
    BinaryMetrics,
    CitationSummary,
    SetMetrics,
    aggregate_code_metrics,
    citation_summary,
    denial_metrics_at_threshold,
    sweep_denial_thresholds,
)
from medilens.phi.screening import PhiDetectedError
from medilens.reasoning.coverage import DETERMINATION_MANUAL_REVIEW
from medilens.reasoning.pipeline import (
    NoApplicablePolicyError,
    ValidationRequest,
    content_reference,
    run_validation,
)
from medilens.reasoning.prompts import PromptTemplate
from medilens.reasoning.verification import GroundingError

STATUS_SCORED = "scored"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"

DEFAULT_DENIAL_THRESHOLD = 0.5
DEFAULT_SWEEP = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


@dataclass(frozen=True)
class CaseResult:
    """The outcome of running one evaluation case."""

    case_id: str
    status: str
    predicted_codes: frozenset[str]
    expected_codes: frozenset[str]
    denial_score: float | None
    expected_denied: bool | None
    determination: str | None
    expected_determination: str | None
    clause_statuses: dict[str, str] = field(default_factory=dict)
    expected_clause_statuses: dict[str, str] = field(default_factory=dict)
    rejection_count: int = 0
    grounding_ok: bool = True
    expect_refusal: bool = False
    detail: str = ""

    def clause_mismatches(self) -> list[tuple[str, str, str | None]]:
        """(clause_id, expected, actual) for every missed clause expectation."""
        mismatches: list[tuple[str, str, str | None]] = []
        for clause_id, expected_status in self.expected_clause_statuses.items():
            actual_status = self.clause_statuses.get(clause_id)
            if actual_status != expected_status:
                mismatches.append((clause_id, expected_status, actual_status))
        return mismatches


def _grounding_ok(code_recommendations) -> bool:
    """Confirm the verification guarantee held: every code has located spans.

    This must always be True (verification never emits an unlocated span). The
    harness checks it so a regression in the safety net would surface as a
    failed invariant rather than a silently wrong metric.
    """
    for recommendation in code_recommendations:
        if len(recommendation.supporting_spans) == 0:
            return False
        for span in recommendation.supporting_spans:
            if not isinstance(span.start_offset, int):
                return False
    return True


def run_case(
    session: Session,
    model_client,
    prompt_template: PromptTemplate,
    case: EvalCase,
) -> CaseResult:
    """Run one case through the pipeline and record its scored outcome."""
    request = ValidationRequest(
        note_text=case.note_text,
        input_reference=content_reference(case.note_text),
        requested_service=case.requested_service,
        date_of_service=case.date_of_service,
        payer_name=case.payer_name,
        source_label=f"eval:{case.case_id}",
    )

    try:
        outcome = run_validation(session, model_client, request, prompt_template)
    except (PhiDetectedError, NoApplicablePolicyError) as refusal:
        return CaseResult(
            case_id=case.case_id,
            status=STATUS_REFUSED,
            predicted_codes=frozenset(),
            expected_codes=case.expected_codes,
            denial_score=None,
            expected_denied=case.expected_denied,
            determination=None,
            expected_determination=case.expected_determination,
            expected_clause_statuses=case.expected_clause_statuses,
            expect_refusal=case.expect_refusal,
            detail=str(refusal),
        )
    except (GroundingError, RuntimeError) as error:
        return CaseResult(
            case_id=case.case_id,
            status=STATUS_ERROR,
            predicted_codes=frozenset(),
            expected_codes=case.expected_codes,
            denial_score=None,
            expected_denied=case.expected_denied,
            determination=None,
            expected_determination=case.expected_determination,
            expected_clause_statuses=case.expected_clause_statuses,
            expect_refusal=case.expect_refusal,
            detail=str(error),
        )

    verified = outcome.verified
    assessment = outcome.assessment
    predicted_codes = frozenset(
        recommendation.code for recommendation in verified.code_recommendations
    )
    clause_statuses: dict[str, str] = {}
    for clause_result in assessment.clause_results:
        clause_statuses[clause_result.clause_id] = clause_result.status

    return CaseResult(
        case_id=case.case_id,
        status=STATUS_SCORED,
        predicted_codes=predicted_codes,
        expected_codes=case.expected_codes,
        denial_score=assessment.denial_risk_score,
        expected_denied=case.expected_denied,
        determination=assessment.determination,
        expected_determination=case.expected_determination,
        clause_statuses=clause_statuses,
        expected_clause_statuses=case.expected_clause_statuses,
        rejection_count=len(verified.rejections),
        grounding_ok=_grounding_ok(verified.code_recommendations),
        expect_refusal=case.expect_refusal,
        detail="",
    )


@dataclass(frozen=True)
class EvaluationReport:
    """Aggregate metrics plus the per-case results that produced them."""

    results: list[CaseResult]
    threshold: float
    code_metrics: SetMetrics
    denial_metrics: BinaryMetrics
    citations: CitationSummary
    scored_count: int
    refused_count: int
    error_count: int
    refusal_expected: int
    refusal_correct: int
    determination_expected: int
    determination_correct: int
    clause_expected: int
    clause_correct: int
    manual_review_count: int

    def scored_denial_pairs(self) -> list[tuple[float, bool]]:
        """(score, expected_denied) for denial-scoreable cases.

        Excludes refusals, cases without a denial label, and manual_review
        determinations (decision 4: needs-human-review is not a prediction).
        """
        pairs: list[tuple[float, bool]] = []
        for result in self.results:
            if result.status != STATUS_SCORED:
                continue
            if result.determination == DETERMINATION_MANUAL_REVIEW:
                continue
            if result.denial_score is None or result.expected_denied is None:
                continue
            pairs.append((result.denial_score, result.expected_denied))
        return pairs

    def threshold_sweep(
        self, thresholds: list[float]
    ) -> list[tuple[float, BinaryMetrics]]:
        return sweep_denial_thresholds(self.scored_denial_pairs(), thresholds)


def evaluate(
    session: Session,
    model_client,
    prompt_template: PromptTemplate,
    cases: list[EvalCase],
    threshold: float = DEFAULT_DENIAL_THRESHOLD,
) -> EvaluationReport:
    """Run every case and compute the section-8 metrics at one threshold."""
    results: list[CaseResult] = []
    for case in cases:
        results.append(run_case(session, model_client, prompt_template, case))

    scored = [result for result in results if result.status == STATUS_SCORED]

    code_pairs = [
        (result.predicted_codes, result.expected_codes) for result in scored
    ]
    code_metrics = aggregate_code_metrics(code_pairs)

    denial_pairs: list[tuple[float, bool]] = []
    manual_review_count = 0
    for result in scored:
        if result.determination == DETERMINATION_MANUAL_REVIEW:
            manual_review_count = manual_review_count + 1
            continue
        if result.denial_score is not None and result.expected_denied is not None:
            denial_pairs.append((result.denial_score, result.expected_denied))
    denial_metrics = denial_metrics_at_threshold(denial_pairs, threshold)

    citations = citation_summary(
        [(result.grounding_ok, result.rejection_count) for result in scored]
    )

    refused = [result for result in results if result.status == STATUS_REFUSED]
    errored = [result for result in results if result.status == STATUS_ERROR]

    refusal_expected = 0
    refusal_correct = 0
    for result in results:
        if result.expect_refusal:
            refusal_expected = refusal_expected + 1
            if result.status == STATUS_REFUSED:
                refusal_correct = refusal_correct + 1

    determination_expected = 0
    determination_correct = 0
    clause_expected = 0
    clause_correct = 0
    for result in scored:
        if result.expected_determination is not None:
            determination_expected = determination_expected + 1
            if result.determination == result.expected_determination:
                determination_correct = determination_correct + 1
        for clause_id, expected_status in result.expected_clause_statuses.items():
            clause_expected = clause_expected + 1
            if result.clause_statuses.get(clause_id) == expected_status:
                clause_correct = clause_correct + 1

    return EvaluationReport(
        results=results,
        threshold=threshold,
        code_metrics=code_metrics,
        denial_metrics=denial_metrics,
        citations=citations,
        scored_count=len(scored),
        refused_count=len(refused),
        error_count=len(errored),
        refusal_expected=refusal_expected,
        refusal_correct=refusal_correct,
        determination_expected=determination_expected,
        determination_correct=determination_correct,
        clause_expected=clause_expected,
        clause_correct=clause_correct,
        manual_review_count=manual_review_count,
    )


def format_report(report: EvaluationReport) -> str:
    """Render an evaluation report for the terminal."""
    lines: list[str] = []
    lines.append("MediLens evaluation report (SYNTHETIC labels, not coder-adjudicated)")
    lines.append("=" * 68)
    lines.append(
        f"cases: {len(report.results)} "
        f"(scored {report.scored_count}, refused {report.refused_count}, "
        f"errored {report.error_count})"
    )
    lines.append("")

    lines.append("Code recommendation accuracy (micro-averaged over scored cases)")
    code = report.code_metrics
    lines.append(
        f"  precision {code.precision:.2f}  recall {code.recall:.2f}  "
        f"f1 {code.f1:.2f}  "
        f"(tp {code.true_positives}, fp {code.false_positives}, "
        f"fn {code.false_negatives})"
    )
    lines.append("")

    if report.determination_expected > 0:
        lines.append(
            "Coverage determination accuracy: "
            f"{report.determination_correct}/{report.determination_expected}"
        )
    if report.clause_expected > 0:
        lines.append(
            "Targeted clause-status accuracy: "
            f"{report.clause_correct}/{report.clause_expected}"
        )
    lines.append("")

    lines.append(
        f"Denial prediction (threshold {report.threshold:.2f}; "
        f"{report.manual_review_count} manual_review case(s) excluded as "
        "needs-human-review, not predictions)"
    )
    denial = report.denial_metrics
    lines.append(
        f"  precision {denial.precision:.2f}  recall {denial.recall:.2f}  "
        f"f1 {denial.f1:.2f}  "
        f"(tp {denial.true_positives}, fp {denial.false_positives}, "
        f"fn {denial.false_negatives}, tn {denial.true_negatives})"
    )
    lines.append("")

    lines.append("Citation correctness")
    citations = report.citations
    guarantee = "held" if citations.grounding_guarantee_held else "VIOLATED"
    lines.append(f"  grounding guarantee: {guarantee}")
    lines.append(
        f"  model clean rate: {citations.model_clean_rate:.2f} "
        f"({citations.clean_cases}/{citations.scored_cases} scored cases had no "
        "dropped or downgraded items)"
    )
    lines.append("")

    if report.refusal_expected > 0:
        lines.append(
            f"Refusal handling: {report.refusal_correct}/{report.refusal_expected} "
            "expected refusals occurred"
        )
        lines.append("")

    lines.append("Denial threshold sweep (score >= threshold predicts denial)")
    lines.append("  thresh  precision  recall  f1")
    for threshold, metrics in report.threshold_sweep(DEFAULT_SWEEP):
        lines.append(
            f"  {threshold:.2f}    {metrics.precision:.2f}       "
            f"{metrics.recall:.2f}    {metrics.f1:.2f}"
        )
    lines.append("")

    lines.append("Per-case")
    for result in report.results:
        if result.status == STATUS_SCORED:
            codes = ", ".join(sorted(result.predicted_codes)) or "(none)"
            expected = ", ".join(sorted(result.expected_codes)) or "(none)"
            score_text = (
                f"{result.denial_score:.2f}"
                if result.denial_score is not None
                else "n/a"
            )
            determination_text = result.determination or "n/a"
            marker = ""
            if (
                result.expected_determination is not None
                and result.determination != result.expected_determination
            ):
                marker = (
                    f"  <-- expected {result.expected_determination}"
                )
            lines.append(
                f"  [{result.status}] {result.case_id}: "
                f"{determination_text} risk {score_text} "
                f"codes [{codes}] expected [{expected}] "
                f"rejections {result.rejection_count}{marker}"
            )
            for clause_id, expected_status, actual_status in result.clause_mismatches():
                lines.append(
                    f"      clause {clause_id}: expected {expected_status}, "
                    f"got {actual_status}"
                )
        else:
            lines.append(
                f"  [{result.status}] {result.case_id}: {result.detail}"
            )
    return "\n".join(lines)

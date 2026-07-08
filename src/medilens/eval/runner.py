"""Run labeled cases through the pipeline and assemble an evaluation report.

The runner reads (retrieval) but never writes: evaluation measures the
pipeline, it does not create audit records. Each case runs through the same
run_validation the CLI and UI use, so the metrics reflect real behavior.

Refusals (PHI, no applicable policy, missing retrieval) are first-class
outcomes here, not errors to swallow: a case is scored, refused, or errored,
and the report counts each so a refusal never silently inflates or deflates a
metric.
"""

from dataclasses import dataclass

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
    rejection_count: int
    grounding_ok: bool
    expect_refusal: bool
    detail: str


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
            rejection_count=0,
            grounding_ok=True,
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
            rejection_count=0,
            grounding_ok=True,
            expect_refusal=case.expect_refusal,
            detail=str(error),
        )

    verified = outcome.verified
    predicted_codes = frozenset(
        recommendation.code for recommendation in verified.code_recommendations
    )
    return CaseResult(
        case_id=case.case_id,
        status=STATUS_SCORED,
        predicted_codes=predicted_codes,
        expected_codes=case.expected_codes,
        denial_score=verified.denial_risk_score,
        expected_denied=case.expected_denied,
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

    def scored_denial_pairs(self) -> list[tuple[float, bool]]:
        """(score, expected_denied) for scored cases with a denial label."""
        pairs: list[tuple[float, bool]] = []
        for result in self.results:
            if result.status != STATUS_SCORED:
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
    for result in scored:
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

    lines.append(f"Denial prediction (threshold {report.threshold:.2f})")
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
        "dropped items)"
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
            lines.append(
                f"  [{result.status}] {result.case_id}: "
                f"predicted [{codes}] expected [{expected}] "
                f"risk {score_text} rejections {result.rejection_count}"
            )
        else:
            lines.append(
                f"  [{result.status}] {result.case_id}: {result.detail}"
            )
    return "\n".join(lines)

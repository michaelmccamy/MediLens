"""Tests for the evaluation harness (dataset, metrics, runner) under policy v2.

Metric correctness is tested with pure inputs. The runner is tested against
the real seeds and a stub model, so no API calls happen and refusal handling,
determination scoring, manual_review exclusion, and clause-status accuracy are
all exercised deterministically.
"""

import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.db.models import Base
from medilens.eval.dataset import EvalCase, load_default_cases
from medilens.eval.metrics import (
    aggregate_code_metrics,
    binary_metrics,
    citation_summary,
    code_set_counts,
    denial_metrics_at_threshold,
    sweep_denial_thresholds,
)
from medilens.eval.runner import (
    STATUS_REFUSED,
    STATUS_SCORED,
    evaluate,
    format_report,
    run_case,
)
from medilens.ingestion import run_ingestion
from medilens.reasoning.prompts import load_prompt_template

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)

MRI_POLICY = "SYN-LUMBAR-MRI-001"

# A tiny note whose spans the stub cites verbatim, so stub output grounds.
NOTE = (
    "Assessment: Lumbar radiculopathy, left L5 distribution for 8 weeks.\n"
    "Exam: Positive straight leg raise on the left.\n"
    "Care: Completed physical therapy without relief.\n"
)
SPAN_A = "Lumbar radiculopathy, left L5 distribution for 8 weeks"
SPAN_B = "Positive straight leg raise on the left"
SPAN_C = "Completed physical therapy without relief"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        run_ingestion(db_session, FIXED_RETRIEVED_AT)
        yield db_session


class SequenceStubModelClient:
    """Returns preset outputs in order, one per model call."""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.index = 0
        self.calls: list[dict[str, Any]] = []

    def create_structured(
        self, system: str, user_content: str, json_schema: dict[str, Any]
    ) -> SimpleNamespace:
        output = self.outputs[self.index]
        self.index = self.index + 1
        self.calls.append({"user_content": user_content})
        return SimpleNamespace(
            data=output,
            model="claude-sonnet-5",
            request_id="req_eval",
            stop_reason="end_turn",
            input_tokens=800,
            output_tokens=200,
        )


def _judgment(clause_id: str, status: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "policy_identifier": MRI_POLICY,
        "clause_id": clause_id,
        "status": status,
        "evidence": evidence,
    }


def _output(
    code: str = "M54.16",
    duration_value: str = "8",
    red_flag: bool = False,
    extra_span: str | None = None,
) -> dict[str, Any]:
    """A grounded v3 output against the MRI policy for the stub NOTE."""
    supporting = [SPAN_A, SPAN_B]
    if extra_span is not None:
        supporting.append(extra_span)
    if red_flag:
        red_flag_judgment = _judgment("red_flag", "satisfied", [SPAN_B])
    else:
        red_flag_judgment = _judgment("red_flag", "insufficient_documentation", [])
    return {
        "extracted_facts": [
            {"fact": "Radiculopathy documented.", "note_span": SPAN_A}
        ],
        "clinical_facts": [
            {
                "key": "symptom_duration",
                "value": duration_value,
                "unit": "weeks",
                "evidence": SPAN_A,
            }
        ],
        "clause_judgments": [
            _judgment("symptom_duration", "satisfied", [SPAN_A]),
            _judgment("conservative_therapy", "satisfied", [SPAN_C]),
            _judgment("objective_findings", "satisfied", [SPAN_B]),
            red_flag_judgment,
        ],
        "code_recommendations": [
            {
                "code": code,
                "code_system": "ICD-10-CM",
                "rationale": "Most specific supported code for the documentation.",
                "supporting_note_spans": supporting,
            }
        ],
        "documentation_gaps": [
            "If clinically accurate, document the symptom duration."
        ],
        "coverage_rationale": "Illustrative narrative.",
    }


def _case(
    case_id: str = "c1",
    requested_service: str = "lumbar MRI",
    payer: str = "Medicare",
    expected_codes: frozenset[str] = frozenset({"M54.16"}),
    expected_denied: bool | None = None,
    expect_refusal: bool = False,
    expected_determination: str | None = None,
    expected_clause_statuses: dict[str, str] | None = None,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        note_text=NOTE,
        requested_service=requested_service,
        date_of_service=datetime.date(2026, 6, 1),
        payer_name=payer,
        expected_codes=expected_codes,
        expected_denied=expected_denied,
        expect_refusal=expect_refusal,
        expected_determination=expected_determination,
        expected_clause_statuses=expected_clause_statuses or {},
        label_rationale="",
    )


# --- metrics: pure functions ---------------------------------------------------


def test_code_set_counts() -> None:
    tp, fp, fn = code_set_counts({"A", "B"}, {"B", "C"})
    assert (tp, fp, fn) == (1, 1, 1)


def test_aggregate_code_metrics_micro_average() -> None:
    metrics = aggregate_code_metrics(
        [
            ({"M54.16"}, {"M54.16"}),  # tp
            ({"M54.16"}, {"M51.16"}),  # fp + fn
        ]
    )
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)


def test_binary_metrics_confusion() -> None:
    metrics = binary_metrics(
        [(True, True), (True, False), (False, True), (False, False)]
    )
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.true_negatives == 1


def test_denial_metrics_at_threshold() -> None:
    scored = [(0.15, False), (0.85, True), (0.41, True)]
    metrics = denial_metrics_at_threshold(scored, 0.5)
    assert metrics.true_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.true_negatives == 1


def test_threshold_sweep_changes_recall() -> None:
    scored = [(0.15, False), (0.41, True), (0.85, True)]
    sweep = dict(sweep_denial_thresholds(scored, [0.4, 0.5]))
    assert sweep[0.5].recall == pytest.approx(0.5)
    assert sweep[0.4].recall == pytest.approx(1.0)


def test_citation_summary_clean_and_guarantee() -> None:
    summary = citation_summary([(True, 0), (True, 2), (True, 0)])
    assert summary.grounding_guarantee_held is True
    assert summary.model_clean_rate == pytest.approx(2 / 3)


# --- dataset ----------------------------------------------------------------


def test_load_default_cases() -> None:
    cases = load_default_cases()

    ids = {case.case_id for case in cases}
    assert "lumbar-mri-supported-medicare" in ids
    assert "lumbar-mri-red-flag-medicare" in ids
    assert "lumbar-rfa-first-medicare" in ids
    assert "knee-injection-no-policy-medicare" in ids
    for case in cases:
        assert "\r" not in case.note_text
        assert case.note_text.endswith("\n")


def test_case_labels_shape() -> None:
    cases = {case.case_id: case for case in load_default_cases()}

    refusal = cases["knee-injection-no-policy-medicare"]
    assert refusal.expect_refusal is True

    red_flag = cases["lumbar-mri-red-flag-medicare"]
    assert red_flag.expected_determination == "meets_criteria"
    assert red_flag.expected_clause_statuses["not_recent_duplicate"] == "not_applicable"

    supported = cases["lumbar-mri-supported-medicare"]
    # The lookback proof case: silent on verifiable history, no red flag.
    assert supported.expected_determination == "manual_review"
    assert supported.expected_denied is None


# --- runner -----------------------------------------------------------------


def test_run_case_scored_records_determination(session: Session) -> None:
    stub = SequenceStubModelClient([_output()])
    template = load_prompt_template()

    result = run_case(
        session, stub, template,
        _case(expected_determination="manual_review",
              expected_clause_statuses={"not_recent_duplicate": "manual_review"}),
    )

    assert result.status == STATUS_SCORED
    assert result.predicted_codes == frozenset({"M54.16"})
    assert result.determination == "manual_review"
    assert result.denial_score == pytest.approx(0.50)
    assert result.clause_statuses["not_recent_duplicate"] == "manual_review"
    assert result.clause_mismatches() == []


def test_run_case_red_flag_meets(session: Session) -> None:
    stub = SequenceStubModelClient([_output(red_flag=True)])
    template = load_prompt_template()

    result = run_case(session, stub, template, _case())

    assert result.determination == "meets_criteria"
    assert result.denial_score == pytest.approx(0.15)


def test_run_case_refused_when_no_policy(session: Session) -> None:
    stub = SequenceStubModelClient([])
    template = load_prompt_template()

    result = run_case(
        session, stub, template,
        _case(
            case_id="knee",
            requested_service="major joint injection, knee",
            expect_refusal=True,
        ),
    )

    assert result.status == STATUS_REFUSED
    assert len(stub.calls) == 0


def test_run_case_counts_fabrication_as_rejection(session: Session) -> None:
    stub = SequenceStubModelClient(
        [_output(extra_span="Prior lumbar fusion in 2019")]
    )
    template = load_prompt_template()

    result = run_case(session, stub, template, _case())

    assert result.status == STATUS_SCORED
    assert result.rejection_count >= 1
    assert result.grounding_ok is True


def test_evaluate_excludes_manual_review_from_denial_metrics(
    session: Session,
) -> None:
    cases = [
        # meets_criteria via red flag: a usable denial pair (expected False).
        _case(case_id="meets", expected_denied=False,
              expected_determination="meets_criteria"),
        # manual_review: must be excluded from denial metrics even though a
        # label is present.
        _case(case_id="manual", expected_denied=True,
              expected_determination="manual_review"),
    ]
    stub = SequenceStubModelClient([_output(red_flag=True), _output()])
    template = load_prompt_template()

    report = evaluate(session, stub, template, cases, threshold=0.5)

    assert report.scored_count == 2
    assert report.manual_review_count == 1
    # Only the meets case enters the denial confusion matrix.
    assert report.denial_metrics.total == 1
    assert report.denial_metrics.true_negatives == 1
    assert report.determination_expected == 2
    assert report.determination_correct == 2


def test_evaluate_scores_clause_expectations(session: Session) -> None:
    cases = [
        _case(
            case_id="clauses",
            expected_determination="manual_review",
            expected_clause_statuses={
                "symptom_duration": "satisfied",
                "not_recent_duplicate": "manual_review",
                "conservative_therapy": "not_satisfied",  # intentionally wrong
            },
        ),
    ]
    stub = SequenceStubModelClient([_output()])
    template = load_prompt_template()

    report = evaluate(session, stub, template, cases, threshold=0.5)

    assert report.clause_expected == 3
    assert report.clause_correct == 2
    rendered = format_report(report)
    assert "SYNTHETIC labels" in rendered
    assert "expected not_satisfied, got satisfied" in rendered


def test_evaluate_counts_refusals(session: Session) -> None:
    cases = [
        _case(case_id="ok", expected_determination="manual_review"),
        _case(
            case_id="refuse",
            requested_service="major joint injection, knee",
            expect_refusal=True,
        ),
    ]
    stub = SequenceStubModelClient([_output()])
    template = load_prompt_template()

    report = evaluate(session, stub, template, cases, threshold=0.5)

    assert report.scored_count == 1
    assert report.refused_count == 1
    assert report.refusal_expected == 1
    assert report.refusal_correct == 1

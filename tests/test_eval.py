"""Tests for the evaluation harness (dataset, metrics, runner).

Metric correctness is tested with pure inputs. The runner is tested against
the real seeds and a stub model, so no API calls happen and refusal handling,
fabrication accounting, and metric aggregation are all exercised deterministically.
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

# A tiny note whose spans the stub cites verbatim, so stub output grounds.
NOTE = (
    "Assessment: Lumbar radiculopathy, left L5 distribution.\n"
    "Exam: Positive straight leg raise on the left.\n"
)
SPAN_A = "Lumbar radiculopathy, left L5 distribution"
SPAN_B = "Positive straight leg raise on the left"


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


def _output(
    code: str = "M54.16",
    spans: tuple[str, ...] = (SPAN_A, SPAN_B),
    score: float = 0.15,
    extra_span: str | None = None,
) -> dict[str, Any]:
    supporting = list(spans)
    if extra_span is not None:
        supporting.append(extra_span)
    return {
        "extracted_facts": [
            {"fact": "Radiculopathy documented.", "note_span": SPAN_A}
        ],
        "code_recommendations": [
            {
                "code": code,
                "code_system": "ICD-10-CM",
                "rationale": "Most specific supported code for the documentation.",
                "supporting_note_spans": supporting,
                "cited_policy_clauses": [
                    {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 3}
                ],
            }
        ],
        "documentation_gaps": [
            "If clinically accurate, document the symptom duration."
        ],
        "denial_risk_score": score,
        "denial_risk_rationale": "Illustrative rationale.",
    }


def _case(
    case_id: str = "c1",
    requested_service: str = "lumbar MRI",
    payer: str = "Medicare",
    expected_codes: frozenset[str] = frozenset({"M54.16"}),
    expected_denied: bool | None = False,
    expect_refusal: bool = False,
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
        label_rationale="",
    )


# --- metrics: code accuracy -------------------------------------------------


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
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)
    assert metrics.f1 == pytest.approx(0.5)


def test_perfect_code_metrics() -> None:
    metrics = aggregate_code_metrics([({"A"}, {"A"}), ({"B"}, {"B"})])
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 1.0


def test_code_metrics_divide_by_zero_is_zero() -> None:
    metrics = aggregate_code_metrics([(set(), set())])
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0


# --- metrics: denial prediction ---------------------------------------------


def test_binary_metrics_confusion() -> None:
    metrics = binary_metrics(
        [(True, True), (True, False), (False, True), (False, False)]
    )
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.true_negatives == 1
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)


def test_denial_metrics_at_threshold() -> None:
    scored = [(0.15, False), (0.80, True), (0.40, True)]
    # Threshold 0.5: 0.15 -> no (tn), 0.80 -> yes (tp), 0.40 -> no (fn).
    metrics = denial_metrics_at_threshold(scored, 0.5)
    assert metrics.true_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.true_negatives == 1
    assert metrics.recall == pytest.approx(0.5)


def test_threshold_sweep_changes_recall() -> None:
    scored = [(0.15, False), (0.45, True), (0.80, True)]
    sweep = dict(sweep_denial_thresholds(scored, [0.4, 0.5]))
    # Lowering the threshold from 0.5 to 0.4 catches the 0.45 denial.
    assert sweep[0.5].recall == pytest.approx(0.5)
    assert sweep[0.4].recall == pytest.approx(1.0)


# --- metrics: citation correctness ------------------------------------------


def test_citation_summary_clean_and_guarantee() -> None:
    summary = citation_summary([(True, 0), (True, 2), (True, 0)])
    assert summary.grounding_guarantee_held is True
    assert summary.clean_cases == 2
    assert summary.model_clean_rate == pytest.approx(2 / 3)


def test_citation_summary_flags_broken_guarantee() -> None:
    summary = citation_summary([(True, 0), (False, 0)])
    assert summary.grounding_guarantee_held is False


# --- dataset ----------------------------------------------------------------


def test_load_default_cases() -> None:
    cases = load_default_cases()

    ids = {case.case_id for case in cases}
    assert "lumbar-mri-supported-medicare" in ids
    assert "rfa-no-policy-medicare" in ids
    for case in cases:
        # Notes are normalized on load (trailing newline, no CRLF).
        assert "\r" not in case.note_text
        assert case.note_text.endswith("\n")


def test_refusal_case_labels() -> None:
    cases = {case.case_id: case for case in load_default_cases()}
    refusal = cases["rfa-no-policy-medicare"]
    assert refusal.expect_refusal is True
    assert refusal.expected_denied is None


# --- runner -----------------------------------------------------------------


def test_run_case_scored(session: Session) -> None:
    stub = SequenceStubModelClient([_output(score=0.15)])
    template = load_prompt_template()

    result = run_case(session, stub, template, _case())

    assert result.status == STATUS_SCORED
    assert result.predicted_codes == frozenset({"M54.16"})
    assert result.denial_score == pytest.approx(0.15)
    assert result.grounding_ok is True
    assert result.rejection_count == 0


def test_run_case_refused_when_no_policy(session: Session) -> None:
    # Radiofrequency ablation has no loaded Medicare policy: refuse before the
    # model is called.
    stub = SequenceStubModelClient([])
    template = load_prompt_template()

    result = run_case(
        session, stub, template,
        _case(
            case_id="rfa",
            requested_service="radiofrequency ablation, lumbar facet",
            expected_denied=None,
            expect_refusal=True,
        ),
    )

    assert result.status == STATUS_REFUSED
    assert len(stub.calls) == 0
    assert result.predicted_codes == frozenset()


def test_run_case_counts_fabrication_as_rejection(session: Session) -> None:
    # The model cites one real span and one fabricated span; verification drops
    # the fabricated one and records a rejection, but the code survives.
    stub = SequenceStubModelClient(
        [_output(spans=(SPAN_A,), extra_span="Prior lumbar fusion in 2019")]
    )
    template = load_prompt_template()

    result = run_case(session, stub, template, _case())

    assert result.status == STATUS_SCORED
    assert result.predicted_codes == frozenset({"M54.16"})
    assert result.rejection_count >= 1
    # The guarantee still holds: nothing ungrounded was emitted.
    assert result.grounding_ok is True


def test_evaluate_aggregates_metrics(session: Session) -> None:
    # Two scored cases: one correct code + low risk (not denied), one wrong
    # code + high risk (denied). Exercises code and denial aggregation.
    cases = [
        _case(case_id="hit", expected_codes=frozenset({"M54.16"}), expected_denied=False),
        _case(case_id="miss", expected_codes=frozenset({"M51.16"}), expected_denied=True),
    ]
    stub = SequenceStubModelClient([_output(score=0.15), _output(score=0.80)])
    template = load_prompt_template()

    report = evaluate(session, stub, template, cases, threshold=0.5)

    assert report.scored_count == 2
    # Code: one hit (M54.16), one wrong (predicted M54.16, expected M51.16).
    assert report.code_metrics.true_positives == 1
    assert report.code_metrics.false_positives == 1
    assert report.code_metrics.false_negatives == 1
    # Denial: 0.15 vs not-denied (tn), 0.80 vs denied (tp).
    assert report.denial_metrics.true_positives == 1
    assert report.denial_metrics.true_negatives == 1
    assert report.denial_metrics.precision == pytest.approx(1.0)
    assert report.denial_metrics.recall == pytest.approx(1.0)
    # Report renders without error and carries the honesty caveat.
    rendered = format_report(report)
    assert "SYNTHETIC labels" in rendered


def test_evaluate_counts_refusals(session: Session) -> None:
    cases = [
        _case(case_id="ok"),
        _case(
            case_id="refuse",
            requested_service="radiofrequency ablation, lumbar facet",
            expected_denied=None,
            expect_refusal=True,
        ),
    ]
    # Only the first case reaches the model.
    stub = SequenceStubModelClient([_output()])
    template = load_prompt_template()

    report = evaluate(session, stub, template, cases, threshold=0.5)

    assert report.scored_count == 1
    assert report.refused_count == 1
    assert report.refusal_expected == 1
    assert report.refusal_correct == 1

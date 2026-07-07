"""Tests for the recommendation display contract and the labeled sample.

These cover the pure-Python view layer (no Streamlit), so they run in CI
without the ui extra installed.
"""

import datetime

from medilens.reasoning.pipeline import ValidationOutcome, ValidationRequest
from medilens.reasoning.verification import (
    LocatedSpan,
    VerifiedClauseCitation,
    VerifiedCodeRecommendation,
    VerifiedFact,
    VerifiedValidation,
)
from medilens.ui.recommendation_view import (
    build_sample_recommendation,
    view_from_outcome,
    _find_span,
)

GENERATED_AT = datetime.datetime(2026, 2, 1, 9, 0, 0)


def _build(note_text: str = "") -> object:
    return build_sample_recommendation(
        note_text=note_text,
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
        generated_at=GENERATED_AT,
    )


def test_sample_is_flagged_as_sample() -> None:
    recommendation = _build()

    assert recommendation.is_sample is True
    # The provenance must not imply a real model ran.
    assert recommendation.model_name.startswith("SAMPLE")
    assert recommendation.prompt_template_version == "none"


def test_sample_echoes_request_inputs() -> None:
    recommendation = _build()

    assert recommendation.requested_service == "lumbar MRI"
    assert recommendation.date_of_service == datetime.date(2026, 6, 1)
    assert recommendation.payer_name == "Medicare"


def test_sample_code_is_grounded() -> None:
    recommendation = _build()

    assert len(recommendation.code_suggestions) == 1
    suggestion = recommendation.code_suggestions[0]
    assert suggestion.code == "M54.16"
    # Grounding and provenance (guardrail 4): note spans and a policy clause.
    assert len(suggestion.supporting_note_spans) > 0
    assert len(suggestion.cited_policy_clauses) > 0


def test_documentation_gaps_are_conditional() -> None:
    recommendation = _build()

    # Guardrail 1: documentation suggestions must be conditional on accuracy.
    assert len(recommendation.documentation_gaps) > 0
    for gap in recommendation.documentation_gaps:
        assert "clinically accurate" in gap


def test_spans_locate_in_matching_note() -> None:
    note_text = (
        "Chief complaint: Low back pain radiating to left leg, 8 weeks duration."
    )
    recommendation = _build(note_text=note_text)

    suggestion = recommendation.code_suggestions[0]
    located = [span for span in suggestion.supporting_note_spans if span.is_located]
    assert len(located) >= 1
    for span in located:
        assert note_text[span.start_offset : span.end_offset] == span.text


def test_spans_unlocated_when_note_differs() -> None:
    recommendation = _build(note_text="A completely unrelated note body.")

    suggestion = recommendation.code_suggestions[0]
    for span in suggestion.supporting_note_spans:
        assert not span.is_located


def test_view_from_outcome_maps_verified_result_without_alteration() -> None:
    verified = VerifiedValidation(
        extracted_facts=[
            VerifiedFact(
                fact="Radicular pain for 8 weeks.",
                span=LocatedSpan(text="8 weeks", start_offset=10, end_offset=17),
            )
        ],
        code_recommendations=[
            VerifiedCodeRecommendation(
                code="M54.16",
                code_system="ICD-10-CM",
                description="Radiculopathy, lumbar region",
                rationale="Most specific supported code.",
                supporting_spans=[
                    LocatedSpan(text="8 weeks", start_offset=10, end_offset=17)
                ],
                cited_clauses=[
                    VerifiedClauseCitation(
                        policy_identifier="SYN-LUMBAR-MRI-001",
                        clause_number=3,
                        clause_text="Objective neurologic findings documented.",
                    )
                ],
            )
        ],
        documentation_gaps=["If clinically accurate, document prior imaging."],
        denial_risk_score=0.2,
        denial_risk_rationale="Clauses 1 through 3 satisfied.",
        rejections=["dropped code X: not in the candidate set"],
    )
    outcome = ValidationOutcome(
        verified=verified,
        model_name="claude-sonnet-5",
        prompt_template_version="validation_v1",
        request_id="req_test",
        input_tokens=900,
        output_tokens=250,
    )
    request = ValidationRequest(
        note_text="note",
        input_reference="ui-note-abc123",
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )

    view = view_from_outcome(request, outcome, GENERATED_AT)

    assert view.is_sample is False
    assert view.input_reference == "ui-note-abc123"
    assert view.model_name == "claude-sonnet-5"
    assert view.prompt_template_version == "validation_v1"
    suggestion = view.code_suggestions[0]
    assert suggestion.code == "M54.16"
    assert suggestion.description == "Radiculopathy, lumbar region"
    span = suggestion.supporting_note_spans[0]
    assert span.is_located
    assert (span.start_offset, span.end_offset) == (10, 17)
    clause = suggestion.cited_policy_clauses[0]
    assert clause.policy_identifier == "SYN-LUMBAR-MRI-001"
    assert clause.clause_number == 3
    assert view.documentation_gaps == [
        "If clinically accurate, document prior imaging."
    ]
    assert view.denial_risk_score == 0.2
    assert view.verification_rejections == [
        "dropped code X: not in the candidate set"
    ]


def test_find_span_locates_and_reports_offsets() -> None:
    note = "alpha beta gamma"

    span = _find_span(note, "beta")

    assert span.is_located
    assert note[span.start_offset : span.end_offset] == "beta"


def test_find_span_marks_absent_phrase_unlocated() -> None:
    span = _find_span("alpha beta gamma", "delta")

    assert not span.is_located
    assert span.start_offset is None

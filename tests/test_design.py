"""Tests for the review-surface HTML renderer (medilens.ui.design).

The renderer is pure and untrusted-input-facing: it takes note text and model
output and produces HTML. These tests pin the safety-critical behavior
(escaping, the note-partition invariant) and the compliance strings the design
handoff requires to survive, now including the policy-v2 clause table and the
computed determination hero.
"""

import datetime

from medilens.ui import design
from medilens.ui.recommendation_view import (
    ClauseResultView,
    CodeSuggestion,
    NoteSpan,
    RecommendationView,
)

FIXED_GENERATED_AT = datetime.datetime(2026, 7, 8, 15, 42, 54)


def _clause(
    clause_id: str = "symptom_duration",
    status: str = "satisfied",
    decided_by: str = "rule+model",
    evidence: list[NoteSpan] | None = None,
) -> ClauseResultView:
    return ClauseResultView(
        policy_identifier="SYN-LUMBAR-MRI-001",
        clause_id=clause_id,
        title=clause_id.replace("_", " "),
        status=status,
        decided_by=decided_by,
        detail="rule min_duration: documented 8 weeks >= threshold 6 weeks",
        required=True,
        evidence=evidence or [],
    )


def _make_view(
    note_text: str = "Assessment: Lumbar radiculopathy, left L5.\n",
    is_sample: bool = False,
    determination: str = "meets_criteria",
    documentation_gaps: list[str] | None = None,
    verification_rejections: list[str] | None = None,
    code_suggestions: list[CodeSuggestion] | None = None,
    clause_results: list[ClauseResultView] | None = None,
    model_coverage_rationale: str = "Model narrative.",
) -> RecommendationView:
    if documentation_gaps is None:
        documentation_gaps = [
            "If clinically accurate, document prior imaging for this episode."
        ]
    if verification_rejections is None:
        verification_rejections = []
    if code_suggestions is None:
        located = note_text.find("Lumbar radiculopathy")
        span = NoteSpan(
            text="Lumbar radiculopathy",
            start_offset=located if located != -1 else None,
            end_offset=(located + len("Lumbar radiculopathy")) if located != -1 else None,
        )
        code_suggestions = [
            CodeSuggestion(
                code="M54.16",
                code_system="ICD-10-CM",
                description="Radiculopathy, lumbar region",
                rationale="Most specific supported code for documented radiculopathy.",
                supporting_note_spans=[span],
            )
        ]
    if clause_results is None:
        clause_results = [_clause()]
    return RecommendationView(
        is_sample=is_sample,
        input_reference="note-abc123",
        requested_service="Lumbar MRI without contrast",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
        extracted_facts=["Low back pain, 8 weeks.", "Positive SLR on the left."],
        code_suggestions=code_suggestions,
        documentation_gaps=documentation_gaps,
        determination=determination,
        denial_risk_score=0.15,
        determination_rationale="Computed from clause statuses.",
        model_coverage_rationale=model_coverage_rationale,
        model_name="claude-sonnet-5",
        model_version="claude-sonnet-5",
        prompt_template_version="validation_v3",
        generated_at=FIXED_GENERATED_AT,
        clause_results=clause_results,
        verification_rejections=verification_rejections,
    )


# --- safety: HTML escaping of untrusted note and model output ---------------


def test_note_text_is_html_escaped() -> None:
    view = _make_view()
    malicious_note = "Assessment: <script>alert('xss')</script> & bad\n"
    html = design.build_results_html(view, malicious_note, audit_id=1)

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; bad" in html


def test_model_output_is_html_escaped() -> None:
    evil_code = CodeSuggestion(
        code="<img src=x onerror=alert(1)>",
        code_system="ICD-10-CM",
        description="<b>evil</b>",
        rationale="rationale with <script>",
        supporting_note_spans=[],
    )
    view = _make_view(code_suggestions=[evil_code])
    html = design.build_results_html(view, "note\n", audit_id=1)

    # Model-supplied markup is neutralized. (The only <script> in the document
    # is the renderer's own static highlight handler, below the results.)
    assert "<img src=x" not in html
    assert "rationale with <script>" not in html
    assert "&lt;img" in html
    assert "&lt;script&gt;" in html


def test_clause_detail_and_narrative_escaped() -> None:
    clause = _clause()
    clause.detail = "detail with <script>bad</script>"
    view = _make_view(
        clause_results=[clause],
        model_coverage_rationale="narrative <img src=x>",
    )
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "<script>bad" not in html
    assert "narrative <img" not in html


# --- note partition invariant -----------------------------------------------


def test_build_note_segments_concatenate_to_original() -> None:
    note = "Positive SLR on the left. Diminished sensation in the L5 dermatome."
    spans = [("a", 0, 24), ("b", 26, 66)]
    segments = design.build_note_segments(note, spans)

    rebuilt = "".join(text for text, _ids in segments)
    assert rebuilt == note


def test_build_note_segments_handles_overlap() -> None:
    note = "abcdefgh"
    spans = [("x", 1, 5), ("y", 3, 7)]
    segments = design.build_note_segments(note, spans)

    rebuilt = "".join(text for text, _ids in segments)
    assert rebuilt == note
    both = "".join(text for text, ids in segments if "x" in ids and "y" in ids)
    assert both == "de"


def test_build_note_segments_clamps_out_of_range() -> None:
    note = "short"
    spans = [("x", 2, 999)]
    segments = design.build_note_segments(note, spans)

    assert "".join(text for text, _ in segments) == note


# --- conditional gap phrasing (guardrail 1) ---------------------------------


def test_split_conditional_gap_detects_prefix() -> None:
    prefix, remainder = design.split_conditional_gap(
        "If clinically accurate, document duration."
    )

    assert prefix == "If clinically accurate,"
    assert remainder == "document duration."


def test_gap_prefix_is_bolded_in_output() -> None:
    view = _make_view(
        documentation_gaps=["If clinically accurate, document prior imaging."]
    )
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "<strong" in html
    assert "If clinically accurate," in html


# --- determination display ----------------------------------------------------


def test_determination_display_labels() -> None:
    assert design.determination_display("meets_criteria")[0] == "Meets criteria"
    assert design.determination_display("does_not_meet")[0] == "Does not meet"
    assert (
        design.determination_display("insufficient_documentation")[0]
        == "Insufficient documentation"
    )
    assert design.determination_display("manual_review")[0] == "Needs human review"


def test_hero_shows_determination_and_computed_caption() -> None:
    view = _make_view(determination="meets_criteria")
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "Meets criteria" in html
    assert "computed from clause statuses" in html
    assert "Computed from clause statuses." in html  # the rationale paragraph


def test_manual_review_hero_disclaims_prediction() -> None:
    view = _make_view(determination="manual_review")
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "Needs human review" in html
    assert "not a denial prediction" in html


# --- clause table ---------------------------------------------------------------


def test_clause_table_renders_statuses_and_decided_by() -> None:
    clauses = [
        _clause("symptom_duration", "satisfied", "rule+model"),
        _clause("not_recent_duplicate", "manual_review", "deferred"),
        _clause("conservative_therapy", "not_applicable", "override"),
    ]
    view = _make_view(clause_results=clauses)
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "Coverage determination by clause" in html
    assert "symptom_duration" in html
    assert "manual review" in html  # status label with underscores replaced
    assert "not applicable" in html
    assert "deferred" in html
    assert "override" in html
    assert "the model never asserts the overall determination" in html.lower()


def test_clause_evidence_is_clickable_and_highlights_note() -> None:
    note = "Assessment: Lumbar radiculopathy, left L5.\n"
    evidence = NoteSpan(text="Lumbar radiculopathy", start_offset=12, end_offset=32)
    clauses = [_clause("symptom_duration", "satisfied", evidence=[evidence])]
    view = _make_view(clause_results=clauses, code_suggestions=[])
    html = design.build_results_html(view, note, audit_id=1)

    assert 'data-span="c0-0"' in html
    assert 'data-spans="c0-0"' in html


# --- compliance strings survive ------------------------------------------------


def test_results_document_carries_compliance_copy() -> None:
    view = _make_view()
    html = design.build_results_html(view, "Assessment: Lumbar radiculopathy.\n", 9)

    assert "Most accurate supported code, never the highest paying one" in html
    assert "The tool never invents clinical facts" in html
    assert "Every recommendation is reconstructable for audit" in html
    assert "Audit records are append only" in html
    assert design.FOOTER_SENTENCE in html
    assert "claude-sonnet-5" in html
    assert "validation_v3" in html
    assert "audit id: 9" in html


def test_honesty_banner_string_present() -> None:
    html = design.honesty_banner_html()
    assert "based only on documentation currently present" in html
    assert "nothing is ever submitted to a payer" in html


def test_no_em_dash_in_rendered_output() -> None:
    view = _make_view()
    html = design.build_results_html(view, "note\n", audit_id=1)
    # Built from a code point so this test file contains no literal em dash.
    assert chr(0x2014) not in html


# --- state-dependent rendering ----------------------------------------------


def test_sample_view_shows_sample_banner() -> None:
    view = _make_view(is_sample=True)
    html = design.build_results_html(view, "note\n", audit_id=None)
    assert "SAMPLE OUTPUT" in html
    assert "audit id: not stored (sample)" in html


def test_model_narrative_card_labeled_as_prose() -> None:
    view = _make_view(model_coverage_rationale="Some prose.")
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "Model narrative" in html
    assert "not from this text" in html


def test_no_narrative_card_when_empty() -> None:
    view = _make_view(model_coverage_rationale="")
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "Model narrative" not in html


def test_rejections_card_present_when_rejections_exist() -> None:
    view = _make_view(verification_rejections=["dropped code S99.999: not in candidate set"])
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "Dropped by verification" in html


def test_empty_codes_shows_no_supported_codes() -> None:
    view = _make_view(code_suggestions=[])
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "No supported codes found" in html


def test_located_code_span_becomes_clickable_chip() -> None:
    note = "Assessment: Lumbar radiculopathy, left L5.\n"
    view = _make_view(note_text=note)
    html = design.build_results_html(view, note, audit_id=1)

    assert 'data-span="s0-0"' in html
    assert 'data-spans="s0-0"' in html


def test_results_height_is_bounded() -> None:
    view = _make_view()
    height = design.results_height(view, "line\n" * 40)
    assert 900 <= height <= 6000

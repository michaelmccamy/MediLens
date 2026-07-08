"""Tests for the review-surface HTML renderer (medilens.ui.design).

The renderer is pure and untrusted-input-facing: it takes note text and model
output and produces HTML. These tests pin the safety-critical behavior
(escaping, the note-partition invariant) and the compliance strings the design
handoff requires to survive the port.
"""

import datetime

from medilens.ui import design
from medilens.ui.recommendation_view import (
    CodeSuggestion,
    NoteSpan,
    PolicyClauseCitation,
    RecommendationView,
)

FIXED_GENERATED_AT = datetime.datetime(2026, 7, 8, 15, 42, 54)


def _make_view(
    note_text: str = "Assessment: Lumbar radiculopathy, left L5.\n",
    is_sample: bool = False,
    has_coverage_basis: bool = True,
    documentation_gaps: list[str] | None = None,
    verification_rejections: list[str] | None = None,
    code_suggestions: list[CodeSuggestion] | None = None,
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
                cited_policy_clauses=[
                    PolicyClauseCitation(
                        policy_identifier="SYN-LUMBAR-MRI-001",
                        clause_number=3,
                        clause_text="Documentation records objective neurologic findings.",
                    )
                ],
                has_coverage_basis=has_coverage_basis,
            )
        ]
    return RecommendationView(
        is_sample=is_sample,
        input_reference="note-abc123",
        requested_service="Lumbar MRI without contrast",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
        extracted_facts=["Low back pain, 8 weeks.", "Positive SLR on the left."],
        code_suggestions=code_suggestions,
        documentation_gaps=documentation_gaps,
        denial_risk_score=0.15,
        denial_risk_rationale="Clauses 1 through 3 satisfied.",
        model_name="claude-sonnet-5",
        model_version="claude-sonnet-5",
        prompt_template_version="validation_v2",
        generated_at=FIXED_GENERATED_AT,
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
        cited_policy_clauses=[],
        has_coverage_basis=False,
    )
    view = _make_view(code_suggestions=[evil_code])
    html = design.build_results_html(view, "note\n", audit_id=1)

    # Model-supplied markup is neutralized. (The only <script> in the document
    # is the renderer's own static highlight handler, below the results.)
    assert "<img src=x" not in html
    assert "rationale with <script>" not in html
    assert "&lt;img" in html
    assert "&lt;script&gt;" in html


def test_rationale_and_gap_escaped() -> None:
    view = _make_view(
        documentation_gaps=["If clinically accurate, document <b>X</b> & Y."]
    )
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "<b>X</b>" not in html
    assert "&lt;b&gt;X&lt;/b&gt;" in html


# --- note partition invariant -----------------------------------------------


def test_build_note_segments_concatenate_to_original() -> None:
    note = "Positive SLR on the left. Diminished sensation in the L5 dermatome."
    spans = [("a", 0, 24), ("b", 26, 66)]
    segments = design.build_note_segments(note, spans)

    rebuilt = "".join(text for text, _ids in segments)
    assert rebuilt == note


def test_build_note_segments_tags_covering_ids() -> None:
    note = "abcdefgh"
    spans = [("x", 2, 5)]
    segments = design.build_note_segments(note, spans)

    covered = "".join(text for text, ids in segments if "x" in ids)
    assert covered == "cde"


def test_build_note_segments_handles_overlap() -> None:
    note = "abcdefgh"
    # Overlapping spans: [1,5) and [3,7). Character 'd','e' (indexes 3,4)
    # are covered by both.
    spans = [("x", 1, 5), ("y", 3, 7)]
    segments = design.build_note_segments(note, spans)

    rebuilt = "".join(text for text, _ids in segments)
    assert rebuilt == note
    both = "".join(text for text, ids in segments if "x" in ids and "y" in ids)
    assert both == "de"


def test_build_note_segments_no_spans() -> None:
    note = "no citations here"
    segments = design.build_note_segments(note, [])

    assert "".join(text for text, _ in segments) == note
    assert all(len(ids) == 0 for _text, ids in segments)


def test_build_note_segments_clamps_out_of_range() -> None:
    # A defensively out-of-range span must not raise or corrupt the text.
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


def test_split_conditional_gap_leaves_nonconditional_untouched() -> None:
    prefix, remainder = design.split_conditional_gap("Document the duration.")

    assert prefix == ""
    assert remainder == "Document the duration."


def test_gap_prefix_is_bolded_in_output() -> None:
    view = _make_view(
        documentation_gaps=["If clinically accurate, document prior imaging."]
    )
    html = design.build_results_html(view, "note\n", audit_id=1)

    assert "<strong" in html
    assert "If clinically accurate," in html
    assert "document prior imaging." in html


# --- risk band --------------------------------------------------------------


def test_risk_band_thresholds() -> None:
    assert design.risk_band(0.0)[0] == "Low"
    assert design.risk_band(0.33)[0] == "Low"
    assert design.risk_band(0.34)[0] == "Moderate"
    assert design.risk_band(0.66)[0] == "Moderate"
    assert design.risk_band(0.67)[0] == "High"
    assert design.risk_band(1.0)[0] == "High"


# --- compliance strings survive the port ------------------------------------


def test_honesty_banner_string_present() -> None:
    html = design.honesty_banner_html()
    assert "based only on documentation currently present" in html
    assert "Do not add documentation unless it is clinically accurate" in html
    assert "nothing is ever submitted to a payer" in html


def test_synthetic_badge_and_dos_note_present() -> None:
    top = design.top_bar_html(live=True, model_name="claude-sonnet-5")
    assert "SYNTHETIC NOTES ONLY" in top
    assert "resolved against the date of service, not today" in design.dos_note_html()


def test_results_document_carries_compliance_copy() -> None:
    view = _make_view()
    html = design.build_results_html(view, "Assessment: Lumbar radiculopathy.\n", 9)

    assert "Most accurate supported code, never the highest paying one" in html
    assert "The tool never invents clinical facts" in html
    assert "Every recommendation is reconstructable for audit" in html
    assert "Audit records are append only" in html
    assert design.FOOTER_SENTENCE in html
    # Provenance fields.
    assert "claude-sonnet-5" in html
    assert "validation_v2" in html
    assert "note-abc123" in html
    assert "audit id: 9" in html


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


def test_no_coverage_basis_shows_warning() -> None:
    view = _make_view(has_coverage_basis=False)
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "No coverage basis" in html
    assert "Documentation-supported only" in html


def test_rejections_card_present_when_rejections_exist() -> None:
    view = _make_view(verification_rejections=["dropped code S99.999: not in candidate set"])
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "Dropped by verification" in html
    assert "dropped code S99.999" in html


def test_no_rejections_card_when_none() -> None:
    view = _make_view(verification_rejections=[])
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "Dropped by verification" not in html


def test_empty_codes_shows_no_supported_codes() -> None:
    view = _make_view(code_suggestions=[])
    html = design.build_results_html(view, "note\n", audit_id=1)
    assert "No supported codes found" in html


def test_located_span_becomes_clickable_chip_and_highlight() -> None:
    note = "Assessment: Lumbar radiculopathy, left L5.\n"
    view = _make_view(note_text=note)
    html = design.build_results_html(view, note, audit_id=1)

    # The located span produces a clickable chip and a matching highlight span
    # sharing the same id.
    assert 'data-span="s0-0"' in html
    assert 'data-spans="s0-0"' in html
    assert "chars " in html


def test_results_height_is_bounded() -> None:
    view = _make_view()
    height = design.results_height(view, "line\n" * 40)
    assert 900 <= height <= 6000

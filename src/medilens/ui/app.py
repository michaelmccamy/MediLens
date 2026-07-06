"""Streamlit review surface for coding recommendations.

A review and demo surface for a human coder, additive to the CLI (the locked
MVP interface). It renders a recommendation in the display contract from
recommendation_view. The reasoning layer is not implemented yet, so this shows
a clearly-labeled SAMPLE: the point tonight is the review UX and the guardrail
framing, not a real analysis.

Run with:
    uv run streamlit run src/medilens/ui/app.py

CLAUDE.md guardrails visible in this surface:
- Guardrail 8 (UI honesty): a persistent note that suggestions are based only on
  documentation currently present, and to add documentation only if clinically
  accurate.
- Human in the loop: every code is a suggestion for a person to review; there is
  no submit action and the tool never sends anything to a payer.
- Grounding and provenance (guardrail 4): each code shows its supporting note
  spans and the cited policy clause.
"""

import datetime
from pathlib import Path

import streamlit as st

from medilens.ui.recommendation_view import (
    RecommendationView,
    build_sample_recommendation,
)

DEFAULT_PAYERS = ["Medicare", "National Commercial Payer A"]

FALLBACK_NOTE = (
    "SYNTHETIC NOTE. Not a real patient. For testing only.\n\n"
    "Chief complaint: Low back pain radiating to left leg, 8 weeks duration.\n"
    "Conservative treatment: Completed 6 weeks of physical therapy and NSAIDs "
    "with minimal improvement.\n"
    "Physical exam: Positive straight leg raise on the left. Diminished "
    "sensation in the left L5 dermatome.\n"
    "Plan: Requesting lumbar MRI without contrast."
)


def _load_default_note() -> str:
    """Prefill the note box with the bundled synthetic fixture when available."""
    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = (
        repo_root
        / "tests"
        / "fixtures"
        / "synthetic_notes"
        / "lumbar_mri_example.txt"
    )
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    return FALLBACK_NOTE


def _render_sample_banner() -> None:
    st.error(
        "SAMPLE OUTPUT. The reasoning layer is not implemented yet, so this "
        "screen shows a fixed illustrative example. It does not analyze the "
        "note. Do not use it for any real coding decision."
    )


def _render_honesty_notes() -> None:
    st.info(
        "This suggestion is based only on documentation currently present in "
        "the note. Do not add documentation unless it is clinically accurate."
    )
    st.caption(
        "Every code below is a recommendation for a certified coder or provider "
        "to review. This tool does not make final coding decisions and never "
        "submits anything to a payer."
    )


def _risk_band(score: float) -> str:
    if score < 0.34:
        return "Low"
    if score < 0.67:
        return "Moderate"
    return "High"


def _render_recommendation(recommendation: RecommendationView) -> None:
    st.subheader("Request")
    st.write(f"Requested service: {recommendation.requested_service}")
    st.write(f"Date of service: {recommendation.date_of_service.isoformat()}")
    st.write(f"Payer: {recommendation.payer_name}")
    st.caption(
        "Code sets and payer policy are resolved against the date of service, "
        "not today."
    )

    st.subheader("Recommended codes")
    for suggestion in recommendation.code_suggestions:
        st.markdown(
            f"**{suggestion.code}** ({suggestion.code_system}): "
            f"{suggestion.description}"
        )
        st.write(suggestion.rationale)

        st.markdown("Supporting note spans:")
        for span in suggestion.supporting_note_spans:
            if span.is_located:
                st.markdown(
                    f'> "{span.text}"  \n'
                    f"characters {span.start_offset} to {span.end_offset}"
                )
            else:
                st.markdown(
                    f'> "{span.text}"  \n'
                    "illustrative span, not located in the current note text"
                )

        st.markdown("Cited policy clauses:")
        for clause in suggestion.cited_policy_clauses:
            st.markdown(
                f"- {clause.policy_identifier}, clause {clause.clause_number}: "
                f"{clause.clause_text}"
            )
        st.divider()

    st.subheader("Extracted facts")
    st.caption("Facts read from the note. The tool never invents clinical facts.")
    for fact in recommendation.extracted_facts:
        st.markdown(f"- {fact}")

    st.subheader("Documentation gaps")
    st.caption(
        "Each item is conditional on clinical accuracy. Do not document "
        "anything that is not clinically true."
    )
    for gap in recommendation.documentation_gaps:
        st.markdown(f"- {gap}")

    st.subheader("Denial risk")
    band = _risk_band(recommendation.denial_risk_score)
    st.progress(recommendation.denial_risk_score)
    st.write(f"{band} ({recommendation.denial_risk_score:.2f})")
    st.write(recommendation.denial_risk_rationale)

    st.subheader("Provenance")
    st.caption("Every recommendation is reconstructable for audit.")
    st.write(f"Model: {recommendation.model_name} ({recommendation.model_version})")
    st.write(f"Prompt template version: {recommendation.prompt_template_version}")
    st.write(f"Generated at: {recommendation.generated_at.isoformat()}")


def main() -> None:
    st.set_page_config(page_title="MediLens review (sample)", layout="wide")
    st.title("MediLens documentation and coding review")
    _render_sample_banner()
    _render_honesty_notes()

    with st.form("review_request"):
        note_text = st.text_area(
            "Clinical note (synthetic, de-identified only)",
            value=_load_default_note(),
            height=300,
        )
        requested_service = st.text_input(
            "Requested service", value="lumbar MRI"
        )
        date_of_service = st.date_input(
            "Date of service", value=datetime.date(2026, 6, 1)
        )
        payer_name = st.selectbox("Payer", DEFAULT_PAYERS)
        submitted = st.form_submit_button("Show sample recommendation")

    if submitted:
        recommendation = build_sample_recommendation(
            note_text=note_text,
            requested_service=requested_service,
            date_of_service=date_of_service,
            payer_name=payer_name,
            generated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        _render_sample_banner()
        _render_recommendation(recommendation)


main()

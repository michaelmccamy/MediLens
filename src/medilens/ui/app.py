"""Streamlit review surface for coding recommendations.

A review surface for a human coder, additive to the CLI (the locked MVP
interface). When the environment is configured (API key and database), a
submitted note runs the real reasoning pipeline: date-resolved retrieval,
model call, grounding verification, and an append-only audit record. When
configuration is missing, it falls back to a clearly-labeled SAMPLE so the
surface is still demonstrable.

Run with:
    uv run streamlit run src/medilens/ui/app.py

CLAUDE.md guardrails visible in this surface:
- Guardrail 8 (UI honesty): a persistent note that suggestions are based only on
  documentation currently present, and to add documentation only if clinically
  accurate.
- Human in the loop: every code is a suggestion for a person to review; there is
  no submit action and the tool never sends anything to a payer.
- Grounding and provenance (guardrail 4): each code shows its supporting note
  spans and the cited policy clause. Output that fails a grounding check is
  shown as an error, never rendered as a recommendation.
"""

import datetime
from pathlib import Path

import streamlit as st

from medilens.reasoning.verification import GroundingError
from medilens.ui.recommendation_view import (
    RecommendationView,
    build_sample_recommendation,
    view_from_outcome,
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


def _try_load_settings():
    """Load settings, returning None (not raising) when configuration is missing.

    The UI degrades to the labeled sample instead of crashing, but never
    silently: the mode banner tells the user which mode they are in.
    """
    from medilens.config import load_settings

    try:
        return load_settings()
    except RuntimeError:
        return None


def _render_sample_banner() -> None:
    st.error(
        "SAMPLE OUTPUT. ANTHROPIC_API_KEY or DATABASE_URL is not configured, "
        "so this screen shows a fixed illustrative example. It does not "
        "analyze the note. Do not use it for any real coding decision."
    )


def _render_live_banner(settings) -> None:
    st.success(
        f"Live mode: model {settings.model_name}, database configured. "
        "Submitted notes are validated by the reasoning pipeline and written "
        "to the audit store. Synthetic, de-identified notes only."
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


def _run_live_validation(
    settings, note_text: str, requested_service: str,
    date_of_service: datetime.date, payer_name: str,
) -> tuple[RecommendationView, int]:
    """Run the real pipeline and persist the result, returning view + audit id.

    Imports are local so the app still renders in sample mode without a
    database driver configured. input_reference is a content hash: an opaque
    pointer to the pasted note, not the note text (CLAUDE.md section 6).
    """
    from medilens.client.anthropic_client import ModelClient
    from medilens.db.session import build_engine, build_session_factory
    from medilens.reasoning.pipeline import (
        ValidationRequest,
        content_reference,
        persist_validation,
        run_validation,
    )
    from medilens.reasoning.prompts import load_prompt_template

    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service=requested_service,
        date_of_service=date_of_service,
        payer_name=payer_name,
        source_label="pasted note in review UI",
    )
    prompt_template = load_prompt_template()
    model_client = ModelClient(settings)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        outcome = run_validation(session, model_client, request, prompt_template)
        created_at = datetime.datetime.now(datetime.timezone.utc)
        recommendation_id = persist_validation(session, request, outcome, created_at)

    view = view_from_outcome(request, outcome, created_at)
    return view, recommendation_id


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
    if len(recommendation.code_suggestions) == 0:
        st.write(
            "No supported codes found in the documentation. See the denial "
            "risk rationale and documentation gaps below."
        )
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
    st.write(f"Input reference: {recommendation.input_reference}")
    st.write(f"Generated at: {recommendation.generated_at.isoformat()}")


def main() -> None:
    st.set_page_config(page_title="MediLens review", layout="wide")
    st.title("MediLens documentation and coding review")

    settings = _try_load_settings()
    if settings is None:
        _render_sample_banner()
    else:
        _render_live_banner(settings)
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
        submitted = st.form_submit_button("Check documentation")

    if not submitted:
        return

    if settings is None:
        recommendation = build_sample_recommendation(
            note_text=note_text,
            requested_service=requested_service,
            date_of_service=date_of_service,
            payer_name=payer_name,
            generated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        _render_sample_banner()
        _render_recommendation(recommendation)
        return

    try:
        with st.spinner("Validating documentation against payer policy..."):
            recommendation, recommendation_id = _run_live_validation(
                settings, note_text, requested_service, date_of_service, payer_name
            )
    except GroundingError as error:
        # Output that fails a grounding gate is never rendered as a
        # recommendation and nothing was stored (guardrails 1 and 4).
        st.error(
            "The model output failed a grounding check and was rejected: "
            f"{error} Nothing was stored. Please retry."
        )
        return
    except RuntimeError as error:
        # Missing retrieval data (for example: seeds not ingested, unknown
        # payer, date outside every effective window).
        st.error(str(error))
        return

    _render_recommendation(recommendation)
    st.caption(f"Audit recommendation id: {recommendation_id}")


main()

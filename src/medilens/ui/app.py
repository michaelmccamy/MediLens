"""Streamlit review surface for coding recommendations.

A review surface for a human coder, additive to the CLI (the locked MVP
interface). When the environment is configured (API key and database), a
submitted note runs the real reasoning pipeline: date-resolved retrieval,
model call, grounding verification, and an append-only audit record. When
configuration is missing, it falls back to a clearly-labeled SAMPLE so the
surface is still demonstrable.

The look is the handoff design (see medilens.ui.design). This module is the
thin host: it owns the Streamlit widgets that capture input and the pipeline
call, then hands the verified RecommendationView to the pure HTML renderer.

Run with:
    uv run streamlit run src/medilens/ui/app.py

CLAUDE.md guardrails visible in this surface:
- Guardrail 8 (UI honesty): a persistent banner that suggestions are based only
  on documentation currently present, and to add documentation only if
  clinically accurate.
- Human in the loop: every code is a suggestion for a person to review; there is
  no submit action and the tool never sends anything to a payer.
- Grounding and provenance (guardrail 4): each code shows its supporting note
  spans and the cited policy clause. Output that fails a grounding check is
  shown as an error, never rendered as a recommendation.
"""

import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from medilens.notes.ingest import (
    load_and_normalize_upload,
    normalize_note_text,
)
from medilens.phi.screening import PhiDetectedError
from medilens.reasoning.pipeline import NoApplicablePolicyError
from medilens.reasoning.verification import GroundingError
from medilens.ui import design
from medilens.ui.recommendation_view import (
    RecommendationView,
    build_sample_recommendation,
    view_from_outcome,
)

# Plain-language service labels (no CPT descriptors, guardrail: CPT out of MVP).
# The first two match loaded policies; the others demonstrate the honest
# "no applicable policy" refusal when no loaded policy governs the service.
SERVICE_OPTIONS = [
    "Lumbar MRI without contrast",
    "Lumbar epidural steroid injection",
    "Major joint injection, knee",
    "Radiofrequency ablation, lumbar facet",
]

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
    silently: the top-bar pill and a sample banner tell the user the mode.
    """
    from medilens.config import load_settings

    try:
        return load_settings()
    except RuntimeError:
        return None


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


def _resolve_note_text(uploaded_file, pasted_text: str) -> str | None:
    """Return the normalized note from the upload (preferred) or pasted text.

    Returns None after surfacing an error card for an unsupported upload, so
    the caller stops without running the pipeline.
    """
    if uploaded_file is not None:
        try:
            return load_and_normalize_upload(
                uploaded_file.name, uploaded_file.getvalue()
            )
        except ValueError as error:
            st.markdown(
                design.error_card_html("Unsupported file", str(error)),
                unsafe_allow_html=True,
            )
            return None
    return normalize_note_text(pasted_text)


def _render_results(
    view: RecommendationView, note_text: str, audit_id: int | None
) -> None:
    """Render the full results document (note panel + results) as a component."""
    html_doc = design.build_results_html(view, note_text, audit_id)
    height = design.results_height(view, note_text)
    components.html(html_doc, height=height, scrolling=True)


def main() -> None:
    st.set_page_config(
        page_title="MediLens · Documentation and coding review",
        layout="wide",
    )
    st.markdown(design.page_css(), unsafe_allow_html=True)

    settings = _try_load_settings()
    live = settings is not None
    model_name = settings.model_name if live else "sample"

    st.markdown(design.top_bar_html(live, model_name), unsafe_allow_html=True)
    st.markdown(design.honesty_banner_html(), unsafe_allow_html=True)

    with st.form("review_request"):
        top = st.columns([2.2, 1.3, 2.0, 1.1])
        with top[0]:
            requested_service = st.selectbox("Requested service", SERVICE_OPTIONS)
        with top[1]:
            date_of_service = st.date_input(
                "Date of service", value=datetime.date(2026, 6, 1)
            )
        with top[2]:
            payer_name = st.selectbox("Payer", DEFAULT_PAYERS)
        with top[3]:
            st.markdown(design.dos_note_html(), unsafe_allow_html=True)

        uploaded_file = st.file_uploader(
            "Upload a note (.txt, .md, or .rtf), or paste one below",
            type=["txt", "md", "rtf"],
        )
        note_text = st.text_area(
            "Clinical note (synthetic, de-identified only)",
            value=_load_default_note(),
            height=220,
        )
        st.caption(
            "If a file is uploaded, it is used instead of the pasted text. "
            "Notes are normalized (unicode, whitespace, line endings) and "
            "screened for PHI before analysis."
        )
        submitted = st.form_submit_button("Run review", type="primary")

    if not submitted:
        return

    resolved_note = _resolve_note_text(uploaded_file, note_text)
    if resolved_note is None:
        return

    if not live:
        recommendation = build_sample_recommendation(
            note_text=resolved_note,
            requested_service=requested_service,
            date_of_service=date_of_service,
            payer_name=payer_name,
            generated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        _render_results(recommendation, resolved_note, audit_id=None)
        return

    try:
        with st.spinner("Validating documentation against payer policy..."):
            recommendation, recommendation_id = _run_live_validation(
                settings, resolved_note, requested_service,
                date_of_service, payer_name,
            )
    except PhiDetectedError as error:
        # Refused before the model was called; the message names PHI categories
        # only, never the values (guardrail 6).
        st.markdown(
            design.error_card_html("Note refused: possible PHI", str(error)),
            unsafe_allow_html=True,
        )
        return
    except NoApplicablePolicyError as error:
        # No loaded policy governs this payer + service. Refused before any
        # model call; the message names which services are loaded.
        st.markdown(
            design.error_card_html("No applicable payer policy", str(error)),
            unsafe_allow_html=True,
        )
        return
    except GroundingError as error:
        # Output that fails a grounding gate is never rendered as a
        # recommendation and nothing was stored (guardrails 1 and 4).
        st.markdown(
            design.error_card_html(
                "Output rejected by grounding check",
                f"{error} Nothing was stored. Please retry.",
            ),
            unsafe_allow_html=True,
        )
        return
    except RuntimeError as error:
        # Missing retrieval data (for example: seeds not ingested, unknown
        # payer, date outside every effective window).
        st.markdown(
            design.error_card_html("Cannot validate", str(error)),
            unsafe_allow_html=True,
        )
        return

    _render_results(recommendation, resolved_note, audit_id=recommendation_id)


main()

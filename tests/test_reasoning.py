"""Tests for the reasoning layer: prompts, pipeline, grounding, regressions.

The model is a stub returning canned structured output, so no API calls
happen; the database is in-memory SQLite loaded with the real curated seeds;
the note is the synthetic lumbar-MRI fixture. This means the grounding checks
run against the same data a real request would use.

Includes the regression checks CLAUDE.md section 8 requires: output that
fabricates a fact not present in the note fixture is rejected, and output
that recommends a code without documentation support (the enforceable
upcoding proxy until fee-schedule data exists) is rejected. In every rejection
case, nothing is persisted.
"""

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.db.models import AuditLogEntry, Base, Recommendation
from medilens.ingestion import run_ingestion
from medilens.reasoning.pipeline import (
    ValidationRequest,
    persist_validation,
    run_validation,
)
from medilens.reasoning.prompts import build_user_content, load_prompt_template
from medilens.reasoning.verification import GroundingError

FIXTURE_NOTE_PATH = (
    Path(__file__).parent / "fixtures" / "synthetic_notes" / "lumbar_mri_example.txt"
)
FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)
FIXED_CREATED_AT = datetime.datetime(2026, 6, 2, 9, 0, 0)

# Verbatim substrings of the fixture note, used as grounded citations.
SPAN_CHIEF_COMPLAINT = "Low back pain radiating to left leg, 8 weeks duration"
SPAN_EXAM = "Positive straight leg raise on the left at 40 degrees"
SPAN_THERAPY = "Completed 6 weeks of physical therapy"


@pytest.fixture
def note_text() -> str:
    return FIXTURE_NOTE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def session() -> Session:
    """In-memory database loaded with the real curated seeds."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        run_ingestion(db_session, FIXED_RETRIEVED_AT)
        yield db_session


class StubModelClient:
    """Returns canned structured output; records what it was sent."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def create_structured(
        self, system: str, user_content: str, json_schema: dict[str, Any]
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "system": system,
                "user_content": user_content,
                "json_schema": json_schema,
            }
        )
        return SimpleNamespace(
            data=self.output,
            model="claude-sonnet-5",
            request_id="req_synthetic_reasoning",
            stop_reason="end_turn",
            input_tokens=900,
            output_tokens=250,
        )


def _make_request(note_text: str) -> ValidationRequest:
    return ValidationRequest(
        note_text=note_text,
        input_reference="tests/fixtures/synthetic_notes/lumbar_mri_example.txt",
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )


def _make_valid_output() -> dict[str, Any]:
    return {
        "extracted_facts": [
            {
                "fact": "Radicular low back pain for 8 weeks.",
                "note_span": SPAN_CHIEF_COMPLAINT,
            },
            {
                "fact": "Completed a conservative therapy trial.",
                "note_span": SPAN_THERAPY,
            },
        ],
        "code_recommendations": [
            {
                "code": "M54.16",
                "code_system": "ICD-10-CM",
                "rationale": (
                    "Most specific supported code for documented lumbar "
                    "radiculopathy with objective exam findings."
                ),
                "supporting_note_spans": [SPAN_CHIEF_COMPLAINT, SPAN_EXAM],
                "cited_policy_clauses": [
                    {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 3}
                ],
            }
        ],
        "documentation_gaps": [
            "If clinically accurate, document the functional limitation caused "
            "by the radicular symptoms."
        ],
        "denial_risk_score": 0.2,
        "denial_risk_rationale": (
            "Duration, conservative therapy, and objective findings satisfy "
            "clauses 1 through 3."
        ),
    }


def _run(
    session: Session, note_text: str, output: dict[str, Any]
) -> tuple[Any, StubModelClient]:
    stub = StubModelClient(output)
    template = load_prompt_template()
    outcome = run_validation(session, stub, _make_request(note_text), template)
    return outcome, stub


# --- prompts ---------------------------------------------------------------


def test_prompt_template_loads_with_version() -> None:
    template = load_prompt_template()

    assert template.name == "validation"
    assert template.version == "v1"
    assert "CANDIDATE CODES" in template.text


def test_missing_prompt_template_fails_loudly() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt_template(version="v999")


def test_user_content_carries_note_codes_and_policies(
    session: Session, note_text: str
) -> None:
    outcome, stub = _run(session, note_text, _make_valid_output())

    sent = stub.calls[0]["user_content"]
    assert SPAN_CHIEF_COMPLAINT in sent
    assert "M54.16" in sent
    assert "SYN-LUMBAR-MRI-001" in sent
    assert "Date of service: 2026-06-01" in sent


# --- happy path ------------------------------------------------------------


def test_pipeline_returns_verified_outcome(session: Session, note_text: str) -> None:
    outcome, stub = _run(session, note_text, _make_valid_output())

    assert len(outcome.verified.code_recommendations) == 1
    recommendation = outcome.verified.code_recommendations[0]
    assert recommendation.code == "M54.16"
    # Description comes from the candidate set, not from the model.
    assert recommendation.description == "Radiculopathy, lumbar region"
    # Spans carry real offsets into the note.
    for span in recommendation.supporting_spans:
        assert note_text[span.start_offset : span.end_offset] == span.text
    # Clause text is resolved from the retrieved policy.
    assert recommendation.cited_clauses[0].clause_number == 3
    assert "neurologic findings" in recommendation.cited_clauses[0].clause_text
    assert outcome.prompt_template_version == "validation_v1"
    assert outcome.model_name == "claude-sonnet-5"


def test_pipeline_persists_to_audit_store(session: Session, note_text: str) -> None:
    outcome, stub = _run(session, note_text, _make_valid_output())

    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert stored is not None
    assert stored.prompt_template_version == "validation_v1"
    assert stored.model_name == "claude-sonnet-5"
    assert (
        stored.input_reference
        == "tests/fixtures/synthetic_notes/lumbar_mri_example.txt"
    )
    audit_count = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.recommendation_id == recommendation_id)
        .count()
    )
    assert audit_count == 1


def test_empty_code_recommendations_is_valid_and_persistable(
    session: Session, note_text: str
) -> None:
    # "The note does not support a code" is an honest, auditable outcome
    # (guardrail 4), not an error.
    output = _make_valid_output()
    output["code_recommendations"] = []
    output["denial_risk_score"] = 0.9

    outcome, stub = _run(session, note_text, output)
    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    assert len(outcome.verified.code_recommendations) == 0
    assert session.get(Recommendation, recommendation_id) is not None


def test_wrapped_line_span_locates_with_true_offsets(
    session: Session, note_text: str
) -> None:
    # The fixture note is hard-wrapped, so this sentence contains a newline:
    # "...physical therapy with\nminimal improvement". A model citing it with
    # a single space must still locate, and the stored text must come from
    # the note itself (including the original line break).
    wrapped_citation = "Completed 6 weeks of physical therapy with minimal improvement"
    assert wrapped_citation not in note_text  # sanity: exact match would fail
    output = _make_valid_output()
    output["extracted_facts"][1]["note_span"] = wrapped_citation

    outcome, stub = _run(session, note_text, output)

    located = outcome.verified.extracted_facts[1].span
    assert note_text[located.start_offset : located.end_offset] == located.text
    assert "\n" in located.text  # the persisted text is the note's own wrapping
    assert located.text.replace("\n", " ") == wrapped_citation


# --- regression checks (CLAUDE.md section 8) --------------------------------


def test_regression_fabricated_fact_is_rejected(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["extracted_facts"].append(
        {
            "fact": "Patient had prior lumbar surgery.",
            "note_span": "Prior L4-L5 discectomy in 2020",
        }
    )

    with pytest.raises(GroundingError, match="fabrication"):
        _run(session, note_text, output)

    assert session.query(Recommendation).count() == 0


def test_regression_paraphrased_span_is_rejected(
    session: Session, note_text: str
) -> None:
    # Even a mild paraphrase (case change) breaks provenance and is rejected.
    output = _make_valid_output()
    output["extracted_facts"][0]["note_span"] = SPAN_CHIEF_COMPLAINT.lower()

    with pytest.raises(GroundingError):
        _run(session, note_text, output)


def test_regression_code_outside_candidate_set_is_rejected(
    session: Session, note_text: str
) -> None:
    # The no-freeform-guessing gate: a code the retrieval layer did not
    # supply is rejected no matter how plausible it looks.
    output = _make_valid_output()
    output["code_recommendations"][0]["code"] = "S99.999"

    with pytest.raises(GroundingError, match="candidate set"):
        _run(session, note_text, output)

    assert session.query(Recommendation).count() == 0


def test_regression_code_without_documentation_support_is_rejected(
    session: Session, note_text: str
) -> None:
    # The upcoding proxy: any recommended code must carry located note-span
    # support. A code with stronger payment but no stronger documentation
    # cannot pass this gate because it has no spans to stand on.
    output = _make_valid_output()
    output["code_recommendations"][0]["supporting_note_spans"] = []

    with pytest.raises(GroundingError, match="supporting note spans"):
        _run(session, note_text, output)


def test_cited_clause_number_must_exist(session: Session, note_text: str) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 99}
    ]

    with pytest.raises(GroundingError, match="clause 99"):
        _run(session, note_text, output)


def test_cited_policy_must_be_in_retrieved_set(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-DOES-NOT-EXIST", "clause_number": 1}
    ]

    with pytest.raises(GroundingError, match="SYN-DOES-NOT-EXIST"):
        _run(session, note_text, output)


def test_code_without_policy_clauses_is_rejected(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = []

    with pytest.raises(GroundingError, match="cites no policy clauses"):
        _run(session, note_text, output)


def test_unconditional_documentation_gap_is_rejected(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["documentation_gaps"] = ["Document symptom duration of 8 weeks."]

    with pytest.raises(GroundingError, match="conditionally"):
        _run(session, note_text, output)


def test_denial_risk_score_out_of_bounds_is_rejected(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["denial_risk_score"] = 1.7

    with pytest.raises(GroundingError, match="denial_risk_score"):
        _run(session, note_text, output)


# --- fail loudly on missing retrieval data ----------------------------------


def test_no_codes_in_force_fails_loudly(session: Session, note_text: str) -> None:
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference="ref",
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2020, 1, 1),
        payer_name="Medicare",
    )

    with pytest.raises(RuntimeError, match="no ICD-10-CM codes in force"):
        run_validation(session, stub, request, template)
    # The model is never called when retrieval comes back empty.
    assert len(stub.calls) == 0


def test_unknown_payer_fails_loudly(session: Session, note_text: str) -> None:
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference="ref",
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Unknown Payer",
    )

    with pytest.raises(RuntimeError, match="policies"):
        run_validation(session, stub, request, template)
    assert len(stub.calls) == 0

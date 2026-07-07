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
    NoApplicablePolicyError,
    ValidationRequest,
    content_reference,
    persist_validation,
    run_validation,
)
from medilens.phi.screening import PhiDetectedError
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
    assert template.version == "v2"
    assert "CANDIDATE CODES" in template.text


def test_prior_prompt_versions_remain_loadable() -> None:
    # Old template files are never edited or deleted, so any audit record's
    # prompt_template_version can be reproduced exactly.
    template = load_prompt_template(version="v1")

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
    assert recommendation.has_coverage_basis
    assert outcome.prompt_template_version == "validation_v2"
    assert outcome.model_name == "claude-sonnet-5"


def test_pipeline_persists_to_audit_store(session: Session, note_text: str) -> None:
    outcome, stub = _run(session, note_text, _make_valid_output())

    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert stored is not None
    assert stored.prompt_template_version == "validation_v2"
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


def test_content_reference_is_bounded_and_stable() -> None:
    long_note = "x" * 100000
    reference = content_reference(long_note)

    assert reference == content_reference(long_note)
    assert len(reference) <= 128
    assert reference.startswith("note-")


def test_long_source_path_does_not_overflow_input_reference(
    session: Session, note_text: str
) -> None:
    # Reproduces the live bug: a long note path used to overflow the 128-char
    # input_reference column. The bounded content reference fixes it, and the
    # original path is preserved in the audit detail via source_label.
    long_path = "C:/" + "nested/" * 40 + "lumbar_mri_example.txt"
    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
        source_label=long_path,
    )
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    outcome = run_validation(session, stub, request, template)

    recommendation_id = persist_validation(
        session, request, outcome, FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert stored is not None
    assert len(stored.input_reference) <= 128
    # The full path is still traceable in the append-only audit detail.
    import json

    audit_entry = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.recommendation_id == recommendation_id)
        .one()
    )
    assert json.loads(audit_entry.detail_json)["source_label"] == long_path


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
#
# Verification is per-item: a claim that fails grounding is DROPPED and the
# reason recorded, while the grounded remainder survives. The section-8
# guarantee is that a fabricated or unsupported claim never appears in the
# verified output, not that the whole response is discarded.


def test_regression_fabricated_fact_never_appears_in_output(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["extracted_facts"].append(
        {
            "fact": "Patient had prior lumbar surgery.",
            "note_span": "Prior L4-L5 discectomy in 2020",
        }
    )

    outcome, stub = _run(session, note_text, output)

    # The fabricated fact is dropped; the grounded facts and the code survive.
    fact_texts = [fact.fact for fact in outcome.verified.extracted_facts]
    assert "Patient had prior lumbar surgery." not in fact_texts
    assert len(outcome.verified.code_recommendations) == 1
    assert any("extracted fact" in reason for reason in outcome.verified.rejections)


def test_regression_paraphrased_span_is_dropped(
    session: Session, note_text: str
) -> None:
    # A case change breaks provenance: the fact carrying it is dropped.
    output = _make_valid_output()
    output["extracted_facts"][0]["note_span"] = SPAN_CHIEF_COMPLAINT.lower()

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.extracted_facts) == 1  # only the second fact
    assert len(outcome.verified.rejections) >= 1


def test_regression_code_outside_candidate_set_is_dropped(
    session: Session, note_text: str
) -> None:
    # No-freeform-guessing: a code the retrieval layer did not supply is
    # dropped, and since it was the only code the output has none.
    output = _make_valid_output()
    output["code_recommendations"][0]["code"] = "S99.999"

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 0
    assert any(
        "S99.999" in reason and "candidate set" in reason
        for reason in outcome.verified.rejections
    )


def test_regression_code_without_documentation_support_is_dropped(
    session: Session, note_text: str
) -> None:
    # The upcoding proxy: a code with no located note-span support is dropped.
    output = _make_valid_output()
    output["code_recommendations"][0]["supporting_note_spans"] = []

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 0
    assert any("no supporting note span" in r for r in outcome.verified.rejections)


def test_fabricated_span_dropped_but_grounded_code_survives(
    session: Session, note_text: str
) -> None:
    # This is the case that used to fail the entire output. A code with one
    # real span and one fabricated span keeps the real span and survives; the
    # fabricated span is dropped and recorded.
    output = _make_valid_output()
    output["code_recommendations"][0]["supporting_note_spans"] = [
        SPAN_CHIEF_COMPLAINT,
        "Prior L4-L5 discectomy in 2020",
    ]

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 1
    recommendation = outcome.verified.code_recommendations[0]
    span_texts = [span.text for span in recommendation.supporting_spans]
    assert SPAN_CHIEF_COMPLAINT in span_texts
    assert "Prior L4-L5 discectomy in 2020" not in span_texts
    assert any("supporting span" in r for r in outcome.verified.rejections)


def test_invalid_clause_dropped_but_code_survives_on_valid_clause(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 3},
        {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 99},
    ]

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 1
    clause_numbers = [
        clause.clause_number
        for clause in outcome.verified.code_recommendations[0].cited_clauses
    ]
    assert clause_numbers == [3]
    assert any("clause 99" in r for r in outcome.verified.rejections)


def test_code_with_only_invalid_clause_survives_without_coverage_basis(
    session: Session, note_text: str
) -> None:
    # Coverage decoupling: the invalid citation is dropped and recorded, but
    # the documentation-supported code survives, explicitly flagged.
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause_number": 99}
    ]

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 1
    recommendation = outcome.verified.code_recommendations[0]
    assert recommendation.cited_clauses == []
    assert not recommendation.has_coverage_basis
    assert any("clause 99" in r for r in outcome.verified.rejections)


def test_cited_policy_not_in_retrieved_set_survives_without_coverage_basis(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-DOES-NOT-EXIST", "clause_number": 1}
    ]

    outcome, stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 1
    assert not outcome.verified.code_recommendations[0].has_coverage_basis
    assert any("SYN-DOES-NOT-EXIST" in r for r in outcome.verified.rejections)


def test_code_with_empty_clauses_survives_flagged_and_persists(
    session: Session, note_text: str
) -> None:
    # The model may legitimately return no clauses when none applies (prompt
    # v2 rule 3). The code survives flagged and the flag reaches the audit
    # record.
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = []

    outcome, stub = _run(session, note_text, output)
    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    assert len(outcome.verified.code_recommendations) == 1
    assert not outcome.verified.code_recommendations[0].has_coverage_basis
    import json

    stored = session.get(Recommendation, recommendation_id)
    stored_codes = json.loads(stored.recommended_codes_json)
    assert stored_codes[0]["has_coverage_basis"] is False


def test_unconditional_documentation_gap_is_dropped(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["documentation_gaps"] = [
        "Document symptom duration of 8 weeks.",  # not conditional, dropped
        "If clinically accurate, document prior imaging.",  # kept
    ]

    outcome, stub = _run(session, note_text, output)

    assert outcome.verified.documentation_gaps == [
        "If clinically accurate, document prior imaging."
    ]
    assert any("documentation gap" in r for r in outcome.verified.rejections)


def test_all_dropped_output_still_persists_with_reasons(
    session: Session, note_text: str
) -> None:
    # If every code is dropped, the outcome is an honest "no supported codes"
    # plus reasons, and it is still storable with the reasons in the audit log.
    output = _make_valid_output()
    output["code_recommendations"][0]["code"] = "S99.999"

    outcome, stub = _run(session, note_text, output)
    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    assert len(outcome.verified.code_recommendations) == 0
    assert len(outcome.verified.rejections) >= 1
    import json

    audit_entry = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.recommendation_id == recommendation_id)
        .one()
    )
    detail = json.loads(audit_entry.detail_json)
    assert len(detail["verification_rejections"]) >= 1


def test_denial_risk_score_out_of_bounds_still_hard_rejects(
    session: Session, note_text: str
) -> None:
    # The one remaining hard stop: an out-of-scale score is structural, not a
    # per-item grounding miss, so it raises rather than dropping an item.
    output = _make_valid_output()
    output["denial_risk_score"] = 1.7

    with pytest.raises(GroundingError, match="denial_risk_score"):
        _run(session, note_text, output)


# --- PHI gate --------------------------------------------------------------


def test_phi_in_note_blocks_before_model_and_retrieval(
    session: Session, note_text: str
) -> None:
    # A note carrying a phone number must be refused before anything is sent
    # to the model, because this deployment is not BAA covered.
    poisoned_note = note_text + "\nContact patient at 415-555-0132.\n"
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=poisoned_note,
        input_reference=content_reference(poisoned_note),
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )

    with pytest.raises(PhiDetectedError):
        run_validation(session, stub, request, template)
    # The model was never called and nothing was persisted.
    assert len(stub.calls) == 0
    assert session.query(Recommendation).count() == 0


# --- service-to-policy matching ----------------------------------------------


def test_service_without_applicable_policy_refuses_before_model_call(
    session: Session, note_text: str
) -> None:
    # Medicare's only seeded policy governs lumbar MRI. Requesting an ESI
    # must refuse before any model call, naming the services that ARE loaded,
    # instead of validating against the inapplicable MRI policy.
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service="lumbar epidural steroid injection",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )

    with pytest.raises(NoApplicablePolicyError) as exc_info:
        run_validation(session, stub, request, template)

    assert len(stub.calls) == 0  # the model was never called
    assert "Lumbar MRI" in str(exc_info.value)  # available services are named
    assert session.query(Recommendation).count() == 0


def test_matching_service_reaches_the_model(session: Session, note_text: str) -> None:
    # The commercial payer's ESI policy matches an ESI request, so validation
    # proceeds and only the matching policy is sent to the model.
    output = _make_valid_output()
    output["code_recommendations"][0]["cited_policy_clauses"] = [
        {"policy_identifier": "SYN-LUMBAR-ESI-001", "clause_number": 1}
    ]
    stub = StubModelClient(output)
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service="lumbar epidural steroid injection",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="National Commercial Payer A",
    )

    outcome = run_validation(session, stub, request, template)

    sent = stub.calls[0]["user_content"]
    assert "SYN-LUMBAR-ESI-001" in sent
    assert "SYN-LUMBAR-MRI-001" not in sent
    assert outcome.verified.code_recommendations[0].has_coverage_basis


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

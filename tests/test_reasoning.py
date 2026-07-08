"""Tests for the reasoning layer: prompts, pipeline, verification, coverage.

The model is a stub returning canned v3 structured output, so no API calls
happen; the database is in-memory SQLite loaded with the real curated seeds;
the note is the synthetic lumbar-MRI fixture. This means the grounding checks
and the clause evaluator run against the same data a real request would use.

Includes the regression checks CLAUDE.md section 8 requires: output that
fabricates evidence not present in the note fixture is dropped or downgraded
(never satisfied), and output that recommends a code without documentation
support is dropped. Under policy schema v2 the tests also pin that the model
cannot decide policy satisfaction: judgments without evidence downgrade,
judgments for non-judgment clauses are ignored, and the determination and
score are computed in code.
"""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.db.models import AuditLogEntry, Base, Recommendation
from medilens.ingestion import run_ingestion
from medilens.phi.screening import PhiDetectedError
from medilens.reasoning.pipeline import (
    NoApplicablePolicyError,
    ValidationRequest,
    content_reference,
    persist_validation,
    run_validation,
)
from medilens.reasoning.prompts import load_prompt_template

FIXTURE_NOTE_PATH = (
    Path(__file__).parent / "fixtures" / "synthetic_notes" / "lumbar_mri_example.txt"
)
FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)
FIXED_CREATED_AT = datetime.datetime(2026, 6, 2, 9, 0, 0)

MRI_POLICY = "SYN-LUMBAR-MRI-001"

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
        input_reference=content_reference(note_text),
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )


def _judgment(
    clause_id: str,
    status: str,
    evidence: list[str],
    policy_identifier: str = MRI_POLICY,
) -> dict[str, Any]:
    return {
        "policy_identifier": policy_identifier,
        "clause_id": clause_id,
        "status": status,
        "evidence": evidence,
    }


def _make_valid_output() -> dict[str, Any]:
    """A fully grounded v3 output for the MRI fixture note."""
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
        "clinical_facts": [
            {
                "key": "symptom_duration",
                "value": "8",
                "unit": "weeks",
                "evidence": SPAN_CHIEF_COMPLAINT,
            }
        ],
        "clause_judgments": [
            _judgment("symptom_duration", "satisfied", [SPAN_CHIEF_COMPLAINT]),
            _judgment("conservative_therapy", "satisfied", [SPAN_THERAPY]),
            _judgment("objective_findings", "satisfied", [SPAN_EXAM]),
            _judgment("red_flag", "insufficient_documentation", []),
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
            }
        ],
        "documentation_gaps": [
            "If clinically accurate, document the functional limitation caused "
            "by the radicular symptoms."
        ],
        "coverage_rationale": (
            "Duration, therapy, and objective findings are documented; prior "
            "imaging recency requires history."
        ),
    }


def _run(session: Session, note_text: str, output: dict[str, Any]):
    stub = StubModelClient(output)
    template = load_prompt_template()
    request = _make_request(note_text)
    outcome = run_validation(session, stub, request, template)
    return outcome, stub


# --- prompts -----------------------------------------------------------------


def test_prompt_template_loads_with_version() -> None:
    template = load_prompt_template()

    assert template.name == "validation"
    assert template.version == "v3"
    assert "CANDIDATE CODES" in template.text
    assert "FACTS TO EXTRACT" in template.text
    assert "CLAUSES TO ASSESS" in template.text


def test_prior_prompt_versions_remain_loadable() -> None:
    # Old template files are never edited or deleted, so any audit record's
    # prompt_template_version can be reproduced exactly.
    for version in ("v1", "v2"):
        template = load_prompt_template(version=version)
        assert template.version == version


def test_missing_prompt_template_fails_loudly() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt_template(version="v999")


def test_user_content_carries_note_codes_and_policy_sections(
    session: Session, note_text: str
) -> None:
    outcome, stub = _run(session, note_text, _make_valid_output())

    sent = stub.calls[0]["user_content"]
    assert SPAN_CHIEF_COMPLAINT in sent
    assert "M54.16" in sent
    assert f"POLICY CONTEXT for {MRI_POLICY}" in sent
    assert f"FACTS TO EXTRACT for {MRI_POLICY}" in sent
    assert "symptom_duration" in sent
    assert f"CLAUSES TO ASSESS for {MRI_POLICY}" in sent
    # History-sourced facts are never requested from the model.
    assert "prior_mri_same_region_12mo" not in sent
    # Only the MRI policy is matched for a lumbar MRI request.
    assert "SYN-LUMBAR-ESI-001" not in sent
    assert "SYN-LUMBAR-RFA-001" not in sent


# --- happy path: verification + computed coverage ------------------------------


def test_valid_output_verifies_and_computes_coverage(
    session: Session, note_text: str
) -> None:
    outcome, _stub = _run(session, note_text, _make_valid_output())

    verified = outcome.verified
    assert len(verified.code_recommendations) == 1
    recommendation = verified.code_recommendations[0]
    assert recommendation.code == "M54.16"
    assert recommendation.description == "Radiculopathy, lumbar region"
    for span in recommendation.supporting_spans:
        assert note_text[span.start_offset : span.end_offset] == span.text

    # The clinical fact parsed as a typed value with the documented unit.
    fact = verified.clinical_facts["symptom_duration"]
    assert fact.value == 8.0
    assert fact.unit == "weeks"

    assessment = outcome.assessment
    statuses = {r.clause_id: r.status for r in assessment.clause_results}
    assert statuses["symptom_duration"] == "satisfied"
    assert statuses["conservative_therapy"] == "satisfied"
    assert statuses["objective_findings"] == "satisfied"
    # The imaging-recency lookback always defers: fail closed.
    assert statuses["not_recent_duplicate"] == "manual_review"
    assert assessment.determination == "manual_review"
    assert assessment.denial_risk_score == 0.50
    assert outcome.prompt_template_version == "validation_v3"
    assert len(verified.rejections) == 0


def test_symptom_duration_decided_by_rule_and_model(
    session: Session, note_text: str
) -> None:
    outcome, _stub = _run(session, note_text, _make_valid_output())

    by_id = {r.clause_id: r for r in outcome.assessment.clause_results}
    duration = by_id["symptom_duration"]
    assert duration.decided_by == "rule+model"
    assert "rule min_duration" in duration.detail
    assert "8 weeks" in duration.detail


def test_unit_conversion_happens_in_code(session: Session, note_text: str) -> None:
    # The model reports the duration as documented ("2" "months"); code
    # converts against the 6-week threshold (60 days >= 42 days).
    output = _make_valid_output()
    output["clinical_facts"][0]["value"] = "2"
    output["clinical_facts"][0]["unit"] = "months"

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["symptom_duration"] == "satisfied"


def test_unconvertible_unit_fails_closed(session: Session, note_text: str) -> None:
    output = _make_valid_output()
    output["clinical_facts"][0]["unit"] = "fortnights"

    outcome, _stub = _run(session, note_text, output)

    assert "symptom_duration" not in outcome.verified.clinical_facts
    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    # Rule half has no fact -> insufficient, even though the judgment said yes.
    assert by_id["symptom_duration"] == "insufficient_documentation"
    assert any("not convertible" in r for r in outcome.verified.rejections)


def test_red_flag_bypasses_prerequisites_end_to_end(
    session: Session, note_text: str
) -> None:
    # Only the red flag is judged satisfied; everything else is silent. The
    # bypass moots the entire prerequisite set including the lookback.
    output = _make_valid_output()
    output["clinical_facts"] = []
    output["clause_judgments"] = [
        _judgment("red_flag", "satisfied", [SPAN_EXAM]),
    ]

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["red_flag"] == "satisfied"
    assert by_id["symptom_duration"] == "not_applicable"
    assert by_id["conservative_therapy"] == "not_applicable"
    assert by_id["objective_findings"] == "not_applicable"
    assert by_id["not_recent_duplicate"] == "not_applicable"
    assert outcome.assessment.determination == "meets_criteria"


# --- the model cannot freestyle satisfaction -----------------------------------


def test_satisfied_without_evidence_downgrades(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["clause_judgments"][1] = _judgment("conservative_therapy", "satisfied", [])

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["conservative_therapy"] == "insufficient_documentation"
    assert any(
        "no satisfied without evidence" in r for r in outcome.verified.rejections
    )


def test_satisfied_with_fabricated_evidence_downgrades(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["clause_judgments"][1] = _judgment(
        "conservative_therapy", "satisfied", ["Completed 12 months of acupuncture"]
    )

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["conservative_therapy"] == "insufficient_documentation"


def test_silent_clause_fails_closed(session: Session, note_text: str) -> None:
    output = _make_valid_output()
    # The model returns no judgment at all for conservative_therapy.
    output["clause_judgments"] = [
        _judgment("symptom_duration", "satisfied", [SPAN_CHIEF_COMPLAINT]),
        _judgment("objective_findings", "satisfied", [SPAN_EXAM]),
    ]

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["conservative_therapy"] == "insufficient_documentation"


def test_judgment_for_non_judgment_clause_is_ignored(
    session: Session, note_text: str
) -> None:
    # not_recent_duplicate is manual_review: the model cannot judge it into
    # any status, and the clause still defers.
    output = _make_valid_output()
    output["clause_judgments"].append(
        _judgment("not_recent_duplicate", "satisfied", [SPAN_CHIEF_COMPLAINT])
    )

    outcome, _stub = _run(session, note_text, output)

    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["not_recent_duplicate"] == "manual_review"
    assert any("not a judgment-bearing clause" in r for r in outcome.verified.rejections)


def test_judgment_for_unknown_clause_is_ignored(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["clause_judgments"].append(
        _judgment("invented_clause", "satisfied", [SPAN_EXAM])
    )

    outcome, _stub = _run(session, note_text, output)

    known_ids = {r.clause_id for r in outcome.assessment.clause_results}
    assert "invented_clause" not in known_ids
    assert any("invented_clause" in r for r in outcome.verified.rejections)


def test_uninvited_clinical_fact_is_ignored(session: Session, note_text: str) -> None:
    # The model must not supply history-sourced or invented fact keys.
    output = _make_valid_output()
    output["clinical_facts"].append(
        {
            "key": "prior_mri_same_region_12mo",
            "value": "0",
            "unit": "",
            "evidence": SPAN_CHIEF_COMPLAINT,
        }
    )

    outcome, _stub = _run(session, note_text, output)

    assert "prior_mri_same_region_12mo" not in outcome.verified.clinical_facts
    assert any(
        "prior_mri_same_region_12mo" in r for r in outcome.verified.rejections
    )
    # The lookback still defers regardless.
    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["not_recent_duplicate"] == "manual_review"


def test_fact_with_fabricated_evidence_is_dropped(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["clinical_facts"][0]["evidence"] = "symptoms for 8 weeks per patient"

    outcome, _stub = _run(session, note_text, output)

    assert "symptom_duration" not in outcome.verified.clinical_facts
    by_id = {r.clause_id: r.status for r in outcome.assessment.clause_results}
    assert by_id["symptom_duration"] == "insufficient_documentation"


def test_unparseable_fact_value_is_dropped(session: Session, note_text: str) -> None:
    output = _make_valid_output()
    output["clinical_facts"][0]["value"] = "about two months"

    outcome, _stub = _run(session, note_text, output)

    assert "symptom_duration" not in outcome.verified.clinical_facts
    assert any("did not parse" in r for r in outcome.verified.rejections)


# --- regression checks (CLAUDE.md section 8) --------------------------------


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

    outcome, _stub = _run(session, note_text, output)

    fact_texts = [fact.fact for fact in outcome.verified.extracted_facts]
    assert "Patient had prior lumbar surgery." not in fact_texts
    assert any("extracted fact" in r for r in outcome.verified.rejections)


def test_regression_code_outside_candidate_set_is_dropped(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["code"] = "S99.999"

    outcome, _stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 0
    assert any(
        "S99.999" in r and "candidate set" in r
        for r in outcome.verified.rejections
    )


def test_regression_code_without_documentation_support_is_dropped(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["supporting_note_spans"] = []

    outcome, _stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 0
    assert any("no supporting note span" in r for r in outcome.verified.rejections)


def test_fabricated_span_dropped_but_grounded_code_survives(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["code_recommendations"][0]["supporting_note_spans"] = [
        SPAN_CHIEF_COMPLAINT,
        "Prior L4-L5 discectomy in 2020",
    ]

    outcome, _stub = _run(session, note_text, output)

    assert len(outcome.verified.code_recommendations) == 1
    span_texts = [
        span.text
        for span in outcome.verified.code_recommendations[0].supporting_spans
    ]
    assert SPAN_CHIEF_COMPLAINT in span_texts
    assert "Prior L4-L5 discectomy in 2020" not in span_texts


def test_unconditional_documentation_gap_is_dropped(
    session: Session, note_text: str
) -> None:
    output = _make_valid_output()
    output["documentation_gaps"] = [
        "Document symptom duration of 8 weeks.",  # not conditional, dropped
        "If clinically accurate, document prior imaging.",  # kept
    ]

    outcome, _stub = _run(session, note_text, output)

    assert outcome.verified.documentation_gaps == [
        "If clinically accurate, document prior imaging."
    ]


def test_wrapped_line_span_locates_with_true_offsets(
    session: Session, note_text: str
) -> None:
    # The fixture note is hard-wrapped; a model citing across the wrap with a
    # space must still locate, and the stored text comes from the note itself.
    wrapped_citation = "Completed 6 weeks of physical therapy with minimal improvement"
    assert wrapped_citation not in note_text  # sanity: exact match would fail
    output = _make_valid_output()
    output["extracted_facts"][1]["note_span"] = wrapped_citation

    outcome, _stub = _run(session, note_text, output)

    located = outcome.verified.extracted_facts[1].span
    assert note_text[located.start_offset : located.end_offset] == located.text
    assert "\n" in located.text


# --- persistence ---------------------------------------------------------------


def test_persist_stores_determination_and_clause_results(
    session: Session, note_text: str
) -> None:
    outcome, _stub = _run(session, note_text, _make_valid_output())

    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert stored.coverage_determination == "manual_review"
    assert stored.denial_risk_score == pytest.approx(0.50)
    assert stored.prompt_template_version == "validation_v3"
    clause_results = json.loads(stored.cited_policy_clauses_json)
    statuses = {entry["clause_id"]: entry["status"] for entry in clause_results}
    assert statuses["not_recent_duplicate"] == "manual_review"
    assert statuses["symptom_duration"] == "satisfied"

    audit_entry = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.recommendation_id == recommendation_id)
        .one()
    )
    detail = json.loads(audit_entry.detail_json)
    assert "determination_rationale" in detail
    assert "model_coverage_rationale" in detail
    assert detail["verification_rejections"] == []


def test_no_codes_outcome_still_persists(session: Session, note_text: str) -> None:
    output = _make_valid_output()
    output["code_recommendations"] = []

    outcome, _stub = _run(session, note_text, output)
    recommendation_id = persist_validation(
        session, _make_request(note_text), outcome, FIXED_CREATED_AT
    )

    assert session.get(Recommendation, recommendation_id) is not None


# --- refusals and fail-loud retrieval -------------------------------------------


def test_phi_in_note_blocks_before_model_and_retrieval(
    session: Session, note_text: str
) -> None:
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
    assert len(stub.calls) == 0
    assert session.query(Recommendation).count() == 0


def test_service_without_applicable_policy_refuses_before_model_call(
    session: Session, note_text: str
) -> None:
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service="major joint injection, knee",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
    )

    with pytest.raises(NoApplicablePolicyError) as exc_info:
        run_validation(session, stub, request, template)

    assert len(stub.calls) == 0
    assert "Lumbar MRI" in str(exc_info.value)


def test_unknown_payer_fails_loudly(session: Session, note_text: str) -> None:
    stub = StubModelClient(_make_valid_output())
    template = load_prompt_template()
    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service="lumbar MRI",
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Unknown Payer",
    )

    with pytest.raises(RuntimeError, match="policies"):
        run_validation(session, stub, request, template)


# --- input references ------------------------------------------------------------


def test_content_reference_is_bounded_and_stable() -> None:
    reference = content_reference("some note text")

    assert reference == content_reference("some note text")
    assert reference.startswith("note-")
    assert len(reference) <= 128
    assert reference != content_reference("different note text")

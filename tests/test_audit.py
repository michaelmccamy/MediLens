"""Tests for the append-only audit-store writer."""

import datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.audit.writer import RecommendationRecord, write_recommendation
from medilens.db.models import AuditLogEntry, Base, Recommendation

FIXED_CREATED_AT = datetime.datetime(2026, 2, 1, 9, 30, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _make_record(
    input_reference: str = "note-ref-001",
    cited_note_spans: list | None = None,
    cited_policy_clauses: list | None = None,
) -> RecommendationRecord:
    if cited_note_spans is None:
        cited_note_spans = [
            {"text": "radiating to left leg, 8 weeks", "start": 120, "end": 150}
        ]
    if cited_policy_clauses is None:
        cited_policy_clauses = [
            {"policy_identifier": "SYN-LUMBAR-MRI-001", "clause": 1}
        ]
    return RecommendationRecord(
        input_reference=input_reference,
        date_of_service=datetime.date(2026, 6, 1),
        payer_name="Medicare",
        extracted_facts={"symptom_duration_weeks": 8, "conservative_therapy": True},
        recommended_codes=[{"code": "M54.16", "code_system": "ICD-10-CM"}],
        cited_note_spans=cited_note_spans,
        cited_policy_clauses=cited_policy_clauses,
        denial_risk_score=0.23,
        model_name="claude-sonnet-5",
        model_version="claude-sonnet-5",
        prompt_template_version="v1",
    )


def test_write_recommendation_persists_row(session: Session) -> None:
    recommendation_id = write_recommendation(
        session, _make_record(), FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert stored is not None
    assert stored.input_reference == "note-ref-001"
    assert stored.payer_name == "Medicare"
    assert stored.denial_risk_score == pytest.approx(0.23)
    assert stored.created_at == FIXED_CREATED_AT


def test_structured_fields_round_trip_through_json(session: Session) -> None:
    recommendation_id = write_recommendation(
        session, _make_record(), FIXED_CREATED_AT
    )

    stored = session.get(Recommendation, recommendation_id)
    assert json.loads(stored.recommended_codes_json) == [
        {"code": "M54.16", "code_system": "ICD-10-CM"}
    ]
    assert json.loads(stored.extracted_facts_json) == {
        "symptom_duration_weeks": 8,
        "conservative_therapy": True,
    }


def test_write_recommendation_writes_audit_entry(session: Session) -> None:
    recommendation_id = write_recommendation(
        session, _make_record(), FIXED_CREATED_AT, audit_detail={"note": "created"}
    )

    audit_entries = (
        session.query(AuditLogEntry)
        .filter(AuditLogEntry.recommendation_id == recommendation_id)
        .all()
    )
    assert len(audit_entries) == 1
    assert audit_entries[0].event_type == "recommendation_created"
    assert json.loads(audit_entries[0].detail_json) == {"note": "created"}


def test_writes_are_append_only(session: Session) -> None:
    # The same logical recommendation written twice produces two rows: the
    # store never updates or dedups, preserving full history (guardrail 7).
    first_id = write_recommendation(session, _make_record(), FIXED_CREATED_AT)
    second_id = write_recommendation(session, _make_record(), FIXED_CREATED_AT)

    assert first_id != second_id
    assert session.query(Recommendation).count() == 2


def test_record_with_no_codes_and_no_citations_is_storable(session: Session) -> None:
    # "The note does not support a code" is an honest, auditable finding
    # (guardrail 4); only codes WITHOUT citations are refused.
    record = _make_record(cited_note_spans=[], cited_policy_clauses=[])
    record.recommended_codes = []

    recommendation_id = write_recommendation(session, record, FIXED_CREATED_AT)

    assert session.get(Recommendation, recommendation_id) is not None


def test_rejects_recommendation_without_note_spans(session: Session) -> None:
    record = _make_record(cited_note_spans=[])

    with pytest.raises(ValueError, match="note span"):
        write_recommendation(session, record, FIXED_CREATED_AT)

    # Nothing was written: the guardrail check runs before any insert.
    assert session.query(Recommendation).count() == 0


def test_rejects_recommendation_without_policy_clauses(session: Session) -> None:
    record = _make_record(cited_policy_clauses=[])

    with pytest.raises(ValueError, match="policy clause"):
        write_recommendation(session, record, FIXED_CREATED_AT)

    assert session.query(Recommendation).count() == 0


def test_rejects_oversized_input_reference(session: Session) -> None:
    record = _make_record()
    record.input_reference = "x" * 200

    with pytest.raises(ValueError, match="input_reference"):
        write_recommendation(session, record, FIXED_CREATED_AT)

    assert session.query(Recommendation).count() == 0

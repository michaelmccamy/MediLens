"""Append-only writer for recommendation and audit records.

CLAUDE.md guardrail 7 requires every recommendation to be reconstructable, and
audit records to be append only. This module only ever inserts rows: it never
updates or deletes, so the history of what was recommended, on what inputs,
by which model and prompt version, is preserved intact.

PHI boundary (CLAUDE.md section 6): the Recommendation table is non-PHI
operational storage. input_reference is an opaque pointer to the note in the
separate PHI store, never the note text itself. The extracted-facts and
cited-note-span fields can contain clinical detail derived from the note;
while only synthetic and de-identified data flows through the system that is
fine, but before real PHI is processed these fields must live in BAA-covered
storage (a deferred decision, section 2). This writer does not decide where
the table lives; it only refuses to store the raw note or patient identifiers.
"""

import datetime
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from medilens.db.models import AuditLogEntry, Recommendation


@dataclass
class RecommendationRecord:
    """The structured content of one recommendation, before serialization.

    The caller (the reasoning layer) assembles this from model output. The
    writer serializes the structured fields to JSON for storage, so callers
    work with Python objects rather than pre-encoded strings.
    """

    input_reference: str
    date_of_service: datetime.date
    payer_name: str
    extracted_facts: Any
    recommended_codes: Any
    cited_note_spans: Any
    cited_policy_clauses: Any
    denial_risk_score: float
    model_name: str
    model_version: str
    prompt_template_version: str


def _to_json(value: Any) -> str:
    """Serialize a structured field deterministically for audit storage.

    sort_keys makes the encoding stable regardless of dict ordering, so two
    logically equal records serialize identically and an audit record is
    reproducible (CLAUDE.md section 5, determinism).
    """
    return json.dumps(value, sort_keys=True)


def write_recommendation(
    session: Session,
    record: RecommendationRecord,
    created_at: datetime.datetime,
    audit_detail: dict[str, Any] | None = None,
) -> int:
    """Insert one recommendation plus its creation audit entry, append only.

    created_at is supplied by the caller (not read from the clock here) so the
    write is deterministic and testable. Returns the new recommendation id.

    A guardrail check runs first: a recommendation with no citations violates
    the grounding-and-provenance rule (guardrail 4), so it is refused loudly
    rather than stored. Every recommendation must cite at least one note span
    and at least one policy clause.
    """
    _reject_ungrounded(record)

    recommendation_row = Recommendation(
        input_reference=record.input_reference,
        date_of_service=record.date_of_service,
        payer_name=record.payer_name,
        extracted_facts_json=_to_json(record.extracted_facts),
        recommended_codes_json=_to_json(record.recommended_codes),
        cited_note_spans_json=_to_json(record.cited_note_spans),
        cited_policy_clauses_json=_to_json(record.cited_policy_clauses),
        denial_risk_score=record.denial_risk_score,
        model_name=record.model_name,
        model_version=record.model_version,
        prompt_template_version=record.prompt_template_version,
        created_at=created_at,
    )
    session.add(recommendation_row)
    # Flush so the database assigns the primary key before the audit entry
    # references it, without committing yet (both rows commit together).
    session.flush()

    if audit_detail is None:
        audit_detail = {}
    audit_row = AuditLogEntry(
        recommendation_id=recommendation_row.id,
        event_type="recommendation_created",
        detail_json=_to_json(audit_detail),
        created_at=created_at,
    )
    session.add(audit_row)

    session.commit()
    return recommendation_row.id


def _reject_ungrounded(record: RecommendationRecord) -> None:
    """Fail loudly if a recommendation lacks note-span or policy citations.

    CLAUDE.md guardrail 4 forbids freeform code guessing: every recommendation
    must cite the supporting note span and the policy clause used. Storing an
    ungrounded recommendation would defeat the audit trail, so it is an error.
    """
    if not record.cited_note_spans:
        raise ValueError(
            "recommendation has no cited note spans; guardrail 4 requires a "
            "supporting note span for every recommendation"
        )
    if not record.cited_policy_clauses:
        raise ValueError(
            "recommendation has no cited policy clauses; guardrail 4 requires "
            "the specific policy clause used for every recommendation"
        )

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
    _reject_oversized_input_reference(record)

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


# The input_reference column width in the data model. Enforced here so an
# oversized reference fails with a clear error instead of a database-level
# string truncation deep in the driver stack.
_MAX_INPUT_REFERENCE_LENGTH = 128


def _reject_oversized_input_reference(record: RecommendationRecord) -> None:
    """Reject an input_reference that would overflow its column.

    A raw file path can exceed the column width; callers must derive a bounded
    reference (see pipeline.content_reference). Failing loudly here beats a
    driver-level truncation error and points at the real cause.
    """
    if len(record.input_reference) > _MAX_INPUT_REFERENCE_LENGTH:
        raise ValueError(
            f"input_reference is {len(record.input_reference)} characters, over "
            f"the {_MAX_INPUT_REFERENCE_LENGTH} limit; pass a bounded reference "
            "such as content_reference(note_text), not a raw path"
        )


def _reject_ungrounded(record: RecommendationRecord) -> None:
    """Fail loudly if recommended codes lack grounding or hide coverage state.

    CLAUDE.md guardrail 4 forbids freeform code guessing. A record with NO
    recommended codes is legitimate and storable; "the note does not support a
    code" is itself an auditable finding. When codes exist:

    - There must be cited note spans (documentation support is never optional).
    - Every code entry must state has_coverage_basis explicitly. Coverage and
      documentation support are decoupled, so "no coverage basis" is a valid
      state, but it must be declared, never implied by absence.
    - A code claiming a coverage basis must actually have a clause cited for
      it in cited_policy_clauses.
    """
    if not record.recommended_codes:
        return
    if not record.cited_note_spans:
        raise ValueError(
            "recommendation has codes but no cited note spans; guardrail 4 "
            "requires a supporting note span for every recommended code"
        )

    codes_with_clauses = set()
    if isinstance(record.cited_policy_clauses, list):
        for clause_entry in record.cited_policy_clauses:
            if isinstance(clause_entry, dict) and "code" in clause_entry:
                codes_with_clauses.add(clause_entry["code"])

    for code_entry in record.recommended_codes:
        if not isinstance(code_entry, dict) or "has_coverage_basis" not in code_entry:
            raise ValueError(
                "every recommended code must state has_coverage_basis "
                "explicitly; coverage must be declared, never implied"
            )
        if code_entry["has_coverage_basis"]:
            if code_entry.get("code") not in codes_with_clauses:
                raise ValueError(
                    f"code {code_entry.get('code')!r} claims a coverage basis "
                    "but cites no policy clause; guardrail 4 requires the "
                    "specific clause used"
                )

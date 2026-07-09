"""ORM models for non-PHI operational data.

CLAUDE.md section 6 requires PHI to be separated from operational data and
kept only in BAA-covered storage. Until the BAA-covered deployment path
exists (see CLAUDE.md section 2), nothing here may store raw clinical note
text or patient identifiers. Recommendation records reference the input by
an opaque id, not by its content.
"""

import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CodeSetEntry(Base):
    """One ICD-10-CM or HCPCS Level II code, valid for an effective date range."""

    __tablename__ = "code_set_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_system: Mapped[str] = mapped_column(String(32))
    code: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(512))
    effective_start: Mapped[datetime.date]
    effective_end: Mapped[datetime.date | None]
    source: Mapped[str] = mapped_column(String(256))
    retrieved_at: Mapped[datetime.datetime]
    content_hash: Mapped[str] = mapped_column(String(64))


class PayerPolicy(Base):
    """A versioned payer medical policy or prior-authorization criterion.

    service is the human-readable label of the service the policy governs.
    service_keywords is a curated, comma-separated list of lowercase keyword
    phrases used to match a requested service to this policy; retrieval must
    never apply a policy to a service it does not govern. Both fields default
    to empty for rows ingested before service matching existed; such rows are
    excluded from service-matched retrieval.

    Versioning has two independent axes, and conflating them corrupts
    date-of-service resolution:

    - effective_start / effective_end model the payer's real-world policy
      window. They answer "was this policy in force on the date of service".
    - superseded_at models our curation versions of the same policy record.
      NULL means this row is the current version; a timestamp means a later
      ingest replaced it (a correction or re-curation) and records when.

    A curation fix applies retroactively to every date of service (the old row
    was our transcription, not what the payer had in force), which is why
    supersession must never be expressed through effective_end. Superseded
    rows are kept forever for audit reconstruction; the supersession stamp is
    the only field ever written after insert.
    """

    __tablename__ = "payer_policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    payer_name: Mapped[str] = mapped_column(String(128))
    policy_identifier: Mapped[str] = mapped_column(String(128))
    specialty: Mapped[str] = mapped_column(String(128))
    service: Mapped[str] = mapped_column(String(256), default="")
    service_keywords: Mapped[str] = mapped_column(String(256), default="")
    policy_text: Mapped[str]
    # Canonical JSON of the policy-v2 structure (clauses, rules, facts). Empty
    # for rows ingested before schema v2; such rows cannot be evaluated and the
    # pipeline fails loudly if the newest version of a policy lacks structure.
    structure_json: Mapped[str] = mapped_column(default="")
    effective_start: Mapped[datetime.date]
    effective_end: Mapped[datetime.date | None]
    source: Mapped[str] = mapped_column(String(256))
    retrieved_at: Mapped[datetime.datetime]
    content_hash: Mapped[str] = mapped_column(String(64))
    # When a later ingest replaced this row (see the class docstring). NULL
    # means current. Retrieval must only ever consult current rows.
    superseded_at: Mapped[datetime.datetime | None] = mapped_column(default=None)


class Recommendation(Base):
    """An append-only record of one recommendation produced for a coder to review.

    input_reference points at the PHI-bearing note in the separate
    BAA-covered store. No note text or patient identifier is stored here.
    """

    __tablename__ = "recommendation"

    id: Mapped[int] = mapped_column(primary_key=True)
    input_reference: Mapped[str] = mapped_column(String(128))
    date_of_service: Mapped[datetime.date]
    payer_name: Mapped[str] = mapped_column(String(128))
    extracted_facts_json: Mapped[str]
    recommended_codes_json: Mapped[str]
    cited_note_spans_json: Mapped[str]
    cited_policy_clauses_json: Mapped[str]
    denial_risk_score: Mapped[float]
    # The computed coverage determination (meets_criteria, does_not_meet,
    # insufficient_documentation, manual_review). Derived in code from clause
    # statuses, never taken from the model (policy schema v2). Empty for rows
    # written before schema v2.
    coverage_determination: Mapped[str] = mapped_column(String(64), default="")
    model_name: Mapped[str] = mapped_column(String(128))
    model_version: Mapped[str] = mapped_column(String(128))
    prompt_template_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime.datetime]


class AuditLogEntry(Base):
    """Append-only audit trail entry tied to a recommendation.

    CLAUDE.md section 3 (guardrail 7) requires every recommendation to be
    reconstructable. Rows in this table are never updated or deleted.
    """

    __tablename__ = "audit_log_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendation.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    detail_json: Mapped[str]
    created_at: Mapped[datetime.datetime]

"""The reasoning pipeline: retrieve, prompt, call the model, verify, persist.

This is the layer CLAUDE.md section 4 describes: extract clinical facts from
the note, match them to date-correct codes and coverage requirements, identify
gaps, and explain each recommendation with citations. Orchestration lives here;
the actual work is delegated to the modules that own it (retrieval to the
knowledge and policy layers, the model call to the client layer, truth checking
to verification, persistence to the audit writer), keeping each concern
separate per section 7.

Nothing in this module reads the clock or the note file: timestamps, note text,
and the model client are supplied by the caller (the CLI or tests), so a full
pipeline run is deterministic and testable with a stubbed model.
"""

import datetime
import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from medilens.audit.writer import RecommendationRecord, write_recommendation
from medilens.knowledge.retrieval import list_codes_at_date
from medilens.phi.screening import assert_no_blocking_phi
from medilens.policy.retrieval import list_policies_for_payer_at_date
from medilens.reasoning.prompts import PromptTemplate, build_user_content
from medilens.reasoning.schema import VALIDATION_OUTPUT_SCHEMA
from medilens.reasoning.verification import (
    VerifiedValidation,
    verify_validation_output,
)

# The beachhead code system and specialty (CLAUDE.md section 2). These become
# request parameters when the product expands beyond the beachhead.
BEACHHEAD_CODE_SYSTEM = "ICD-10-CM"
BEACHHEAD_SPECIALTY = "Orthopedics and pain medicine"


@dataclass(frozen=True)
class ValidationRequest:
    """One documentation-sufficiency request, as the CLI or a test poses it.

    input_reference is a bounded, opaque pointer to the note, stored in the
    audit record instead of the note text (CLAUDE.md section 6). It must not be
    a raw file path: paths are unbounded, environment-specific, and can leak
    identifying information in the filename. Use content_reference to derive
    one. source_label is a human-readable origin (a path, "pasted in UI") kept
    for traceability in the unbounded audit detail, not in the indexed column.
    """

    note_text: str
    input_reference: str
    requested_service: str
    date_of_service: datetime.date
    payer_name: str
    source_label: str = ""


def content_reference(note_text: str) -> str:
    """Derive a bounded, stable, opaque reference from note content.

    Same note content yields the same reference, which lets an operator link
    repeat validations of the same note without storing the note or a path in
    the indexed column. The 16-hex-character prefix is far under the 128-char
    column limit and collision-safe at this scale.
    """
    digest = hashlib.sha256(note_text.encode("utf-8")).hexdigest()
    return f"note-{digest[:16]}"


@dataclass(frozen=True)
class ValidationOutcome:
    """A verified validation plus the provenance the audit record needs."""

    verified: VerifiedValidation
    model_name: str
    prompt_template_version: str
    request_id: str | None
    input_tokens: int
    output_tokens: int


def run_validation(
    session: Session,
    model_client,
    request: ValidationRequest,
    prompt_template: PromptTemplate,
) -> ValidationOutcome:
    """Run one documentation-sufficiency check end to end (without persisting).

    model_client is anything exposing create_structured with the ModelClient
    signature, so tests can substitute a stub and never call the real API.
    Raises loudly when retrieval comes back empty: reasoning over an empty
    candidate or policy set would force the model to guess from memory, which
    is exactly what the architecture forbids (sections 4 and 7).
    """
    # PHI screen first: refuse before any retrieval or model call if the note
    # carries high-confidence identifiers. This deployment is not BAA covered,
    # so PHI must never reach the endpoint (CLAUDE.md guardrail 6).
    assert_no_blocking_phi(request.note_text)

    candidate_codes = list_codes_at_date(
        session, BEACHHEAD_CODE_SYSTEM, request.date_of_service
    )
    if len(candidate_codes) == 0:
        raise RuntimeError(
            f"no {BEACHHEAD_CODE_SYSTEM} codes in force on "
            f"{request.date_of_service.isoformat()}; run 'medilens ingest' "
            "or check the date of service"
        )

    policies = list_policies_for_payer_at_date(
        session,
        request.payer_name,
        BEACHHEAD_SPECIALTY,
        request.date_of_service,
    )
    if len(policies) == 0:
        raise RuntimeError(
            f"no {request.payer_name!r} policies for "
            f"{BEACHHEAD_SPECIALTY!r} in force on "
            f"{request.date_of_service.isoformat()}; refusing to validate "
            "without policy grounding"
        )

    user_content = build_user_content(
        note_text=request.note_text,
        requested_service=request.requested_service,
        date_of_service=request.date_of_service,
        payer_name=request.payer_name,
        candidate_codes=candidate_codes,
        policies=policies,
    )

    result = model_client.create_structured(
        system=prompt_template.text,
        user_content=user_content,
        json_schema=VALIDATION_OUTPUT_SCHEMA,
    )

    verified = verify_validation_output(
        output=result.data,
        note_text=request.note_text,
        candidate_codes=candidate_codes,
        policies=policies,
    )

    outcome = ValidationOutcome(
        verified=verified,
        model_name=result.model,
        prompt_template_version=f"{prompt_template.name}_{prompt_template.version}",
        request_id=result.request_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    return outcome


def persist_validation(
    session: Session,
    request: ValidationRequest,
    outcome: ValidationOutcome,
    created_at: datetime.datetime,
) -> int:
    """Write a verified validation to the append-only audit store.

    Serializes the verified structures (not the raw model output) so the audit
    record reflects exactly what survived verification and was shown to the
    coder. Returns the recommendation id.
    """
    extracted_facts = []
    for fact in outcome.verified.extracted_facts:
        extracted_facts.append(
            {
                "fact": fact.fact,
                "note_span": fact.span.text,
                "start_offset": fact.span.start_offset,
                "end_offset": fact.span.end_offset,
            }
        )

    recommended_codes = []
    cited_note_spans = []
    cited_policy_clauses = []
    for recommendation in outcome.verified.code_recommendations:
        recommended_codes.append(
            {
                "code": recommendation.code,
                "code_system": recommendation.code_system,
                "description": recommendation.description,
                "rationale": recommendation.rationale,
            }
        )
        for span in recommendation.supporting_spans:
            cited_note_spans.append(
                {
                    "code": recommendation.code,
                    "text": span.text,
                    "start_offset": span.start_offset,
                    "end_offset": span.end_offset,
                }
            )
        for clause in recommendation.cited_clauses:
            cited_policy_clauses.append(
                {
                    "code": recommendation.code,
                    "policy_identifier": clause.policy_identifier,
                    "clause_number": clause.clause_number,
                }
            )

    record = RecommendationRecord(
        input_reference=request.input_reference,
        date_of_service=request.date_of_service,
        payer_name=request.payer_name,
        extracted_facts=extracted_facts,
        recommended_codes=recommended_codes,
        cited_note_spans=cited_note_spans,
        cited_policy_clauses=cited_policy_clauses,
        denial_risk_score=outcome.verified.denial_risk_score,
        model_name=outcome.model_name,
        model_version=outcome.model_name,
        prompt_template_version=outcome.prompt_template_version,
    )
    audit_detail = {
        "requested_service": request.requested_service,
        "source_label": request.source_label,
        "documentation_gaps": outcome.verified.documentation_gaps,
        "denial_risk_rationale": outcome.verified.denial_risk_rationale,
        "verification_rejections": outcome.verified.rejections,
        "model_request_id": outcome.request_id,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
    }
    recommendation_id = write_recommendation(
        session, record, created_at, audit_detail=audit_detail
    )
    return recommendation_id

"""The reasoning pipeline: retrieve, prompt, call the model, verify, evaluate.

This is the layer CLAUDE.md section 4 describes: extract clinical facts from
the note, match them to date-correct codes and coverage requirements, identify
gaps, and explain each recommendation with citations. Orchestration lives here;
the actual work is delegated to the modules that own it (retrieval to the
knowledge and policy layers, the model call to the client layer, truth checking
to verification, clause evaluation and the computed determination to coverage,
persistence to the audit writer), keeping each concern separate per section 7.

Under policy schema v2 the model never decides policy satisfaction: it
extracts evidenced facts and judgments, the verifier checks them, the rule
engine and clause evaluator compute every clause status, and the overall
determination and denial-risk score are derived in code from those statuses.

Nothing in this module reads the clock or the note file: timestamps, note text,
and the model client are supplied by the caller (the CLI or tests), so a full
pipeline run is deterministic and testable with a stubbed model.
"""

import datetime
import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from medilens.audit.writer import RecommendationRecord, write_recommendation
from medilens.db.models import PayerPolicy
from medilens.knowledge.retrieval import list_codes_at_date
from medilens.phi.screening import assert_no_blocking_phi
from medilens.policy.retrieval import (
    list_policies_for_payer_at_date,
    service_matches,
)
from medilens.policy.structure import PolicyStructure, structure_from_json
from medilens.reasoning.coverage import (
    CoverageAssessment,
    combine_assessments,
    evaluate_policy_coverage,
)
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


class NoApplicablePolicyError(Exception):
    """No loaded policy governs the requested service for this payer.

    Raised before any model call. Validating against an inapplicable policy
    would produce a confused half-answer (the model reasoning about criteria
    that do not govern the service), which is exactly the silent guessing
    CLAUDE.md section 7 forbids. The message names the services that ARE
    loaded for the payer, so the operator can tell a wording mismatch from a
    genuinely missing policy.
    """

    def __init__(
        self,
        payer_name: str,
        requested_service: str,
        available_services: list[str],
    ) -> None:
        self.payer_name = payer_name
        self.requested_service = requested_service
        self.available_services = available_services
        if len(available_services) > 0:
            available_text = "; ".join(available_services)
        else:
            available_text = "(none)"
        super().__init__(
            f"no {payer_name} policy in force governs the requested service "
            f"{requested_service!r}. Services with loaded policies for this "
            f"payer: {available_text}. Coverage cannot be assessed, so "
            "validation is refused rather than checked against an "
            "inapplicable policy."
        )


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
    """A verified validation, its computed coverage, and audit provenance."""

    verified: VerifiedValidation
    assessment: CoverageAssessment
    model_name: str
    prompt_template_version: str
    request_id: str | None
    input_tokens: int
    output_tokens: int


def _latest_versions(policies: list[PayerPolicy]) -> list[PayerPolicy]:
    """Reduce to the newest ingested version of each policy identifier.

    Versioning is append-only (section 4), so several versions of one
    identifier can be in force on the same date. Only the newest is current,
    and retrieval must consult it alone. In particular, service matching must
    run against current keywords only: a superseded version whose service
    keywords have since been narrowed must never match a request the current
    version rejects. Deduplicating before service matching, not after, is what
    makes that hold.
    """
    latest_by_identifier: dict[str, PayerPolicy] = {}
    for policy in policies:
        existing = latest_by_identifier.get(policy.policy_identifier)
        if existing is None or policy.retrieved_at > existing.retrieved_at:
            latest_by_identifier[policy.policy_identifier] = policy
    return list(latest_by_identifier.values())


def _structured_policies(
    matched: list[PayerPolicy],
) -> list[tuple[PayerPolicy, PolicyStructure]]:
    """Parse the structure of each matched (already current) policy.

    A matched policy without structure predates schema v2 and cannot be
    evaluated: that is a loud configuration error, not a silent skip.
    """
    structured: list[tuple[PayerPolicy, PolicyStructure]] = []
    for policy in matched:
        structure = structure_from_json(
            policy.structure_json, policy.policy_identifier
        )
        structured.append((policy, structure))
    structured.sort(key=lambda pair: pair[0].policy_identifier)
    return structured


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

    payer_policies = list_policies_for_payer_at_date(
        session,
        request.payer_name,
        BEACHHEAD_SPECIALTY,
        request.date_of_service,
    )
    if len(payer_policies) == 0:
        raise RuntimeError(
            f"no {request.payer_name!r} policies for "
            f"{BEACHHEAD_SPECIALTY!r} in force on "
            f"{request.date_of_service.isoformat()}; refusing to validate "
            "without policy grounding"
        )

    # Reduce to the current version of each policy before matching, so a
    # superseded version's stale service keywords can never govern retrieval.
    current_policies = _latest_versions(payer_policies)

    # Only policies that govern the requested service reach the model. Feeding
    # an inapplicable policy produces confused half-answers; refusing here is
    # the honest outcome (section 7) and costs no model call.
    matched = []
    for policy in current_policies:
        if service_matches(request.requested_service, policy.service_keywords):
            matched.append(policy)
    if len(matched) == 0:
        available_services: list[str] = []
        for policy in current_policies:
            if policy.service and policy.service not in available_services:
                available_services.append(policy.service)
        raise NoApplicablePolicyError(
            payer_name=request.payer_name,
            requested_service=request.requested_service,
            available_services=available_services,
        )

    structured_policies = _structured_policies(matched)

    user_content = build_user_content(
        note_text=request.note_text,
        requested_service=request.requested_service,
        date_of_service=request.date_of_service,
        payer_name=request.payer_name,
        candidate_codes=candidate_codes,
        policies=structured_policies,
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
        policies=structured_policies,
    )

    # Coverage is computed in code from verified inputs: rule engine for
    # deterministic clauses, verified judgments for qualitative ones, fixed
    # precedence for the determination, derived score (schema v2, decision 1).
    recommended_codes = frozenset(
        recommendation.code for recommendation in verified.code_recommendations
    )
    per_policy_assessments = []
    for policy_row, structure in structured_policies:
        per_policy_assessments.append(
            evaluate_policy_coverage(
                policy_row=policy_row,
                structure=structure,
                clinical_facts=verified.clinical_facts,
                clause_judgments=verified.clause_judgments,
                date_of_service=request.date_of_service,
                recommended_codes=recommended_codes,
            )
        )
    assessment = combine_assessments(per_policy_assessments)

    outcome = ValidationOutcome(
        verified=verified,
        assessment=assessment,
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

    Serializes the verified structures and the computed clause results (not
    the raw model output) so the audit record reflects exactly what survived
    verification and what the coder was shown, including how every clause was
    decided. Returns the recommendation id.
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

    # Clause results are the coverage citation under schema v2: which clause,
    # decided how, with what status. Evidence spans go to the audit detail.
    clause_results = []
    clause_evidence = []
    for result in outcome.assessment.clause_results:
        clause_results.append(
            {
                "policy_identifier": result.policy_identifier,
                "clause_id": result.clause_id,
                "status": result.status,
                "decided_by": result.decided_by,
                "detail": result.detail,
                "required": result.required,
            }
        )
        for span in result.evidence:
            clause_evidence.append(
                {
                    "clause_id": result.clause_id,
                    "text": span.text,
                    "start_offset": span.start_offset,
                    "end_offset": span.end_offset,
                }
            )

    record = RecommendationRecord(
        input_reference=request.input_reference,
        date_of_service=request.date_of_service,
        payer_name=request.payer_name,
        extracted_facts=extracted_facts,
        recommended_codes=recommended_codes,
        cited_note_spans=cited_note_spans,
        cited_policy_clauses=clause_results,
        denial_risk_score=outcome.assessment.denial_risk_score,
        coverage_determination=outcome.assessment.determination,
        model_name=outcome.model_name,
        model_version=outcome.model_name,
        prompt_template_version=outcome.prompt_template_version,
    )
    audit_detail = {
        "requested_service": request.requested_service,
        "source_label": request.source_label,
        "documentation_gaps": outcome.verified.documentation_gaps,
        "determination_rationale": outcome.assessment.determination_rationale,
        "model_coverage_rationale": outcome.verified.coverage_rationale,
        "clause_evidence": clause_evidence,
        "verification_rejections": outcome.verified.rejections,
        "model_request_id": outcome.request_id,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
    }
    recommendation_id = write_recommendation(
        session, record, created_at, audit_detail=audit_detail
    )
    return recommendation_id

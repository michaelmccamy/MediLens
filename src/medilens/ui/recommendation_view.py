"""Display contract for a coding recommendation, plus a labeled sample.

This is the shape the review UI renders. It mirrors the fields the audit store
persists (see medilens.audit.writer.RecommendationRecord and the Recommendation
model): extracted facts, recommended codes, cited note spans, evaluated clause
results, the computed coverage determination and denial-risk score, and
model/prompt provenance. Building the UI against this contract means pipeline
changes populate the same structure with no UI changes.

Under policy schema v2 the determination and score are COMPUTED from clause
statuses, never taken from the model; the model contributes evidence,
judgments, and a clearly-labeled prose narrative.

IMPORTANT: build_sample_recommendation returns a fixed, illustrative SAMPLE
shown only when configuration is missing. Every such view is marked
is_sample=True and the surfaces label it loudly. This matters in a clinical
tool: unlabeled placeholder output that looks like a real analysis would be
dangerous.

No streamlit import here on purpose, so the contract and the sample are plain
Python and unit-testable without the UI.
"""

import datetime
from dataclasses import dataclass, field

from medilens.reasoning.pipeline import ValidationOutcome, ValidationRequest


@dataclass
class NoteSpan:
    """A span of note text cited as supporting a recommendation.

    start_offset/end_offset are character offsets into the note when the cited
    phrase is found in it; None means the phrase was not located in the
    supplied note and is shown for illustration only.
    """

    text: str
    start_offset: int | None
    end_offset: int | None

    @property
    def is_located(self) -> bool:
        return self.start_offset is not None


@dataclass
class ClauseResultView:
    """One evaluated policy clause: the unit of the coverage determination."""

    policy_identifier: str
    clause_id: str
    title: str
    status: str
    decided_by: str
    detail: str
    required: bool
    evidence: list[NoteSpan] = field(default_factory=list)


@dataclass
class CodeSuggestion:
    """One recommended code with its documentation grounding.

    rationale states why this is the most accurate supported code, never the
    highest paying one (CLAUDE.md guiding principle). Coverage is policy-level
    under schema v2 (see clause_results on the view), so codes carry note
    spans only.
    """

    code: str
    code_system: str
    description: str
    rationale: str
    supporting_note_spans: list[NoteSpan] = field(default_factory=list)


@dataclass
class RecommendationView:
    """Everything the review surface displays for one request."""

    is_sample: bool
    input_reference: str
    requested_service: str
    date_of_service: datetime.date
    payer_name: str
    extracted_facts: list[str]
    code_suggestions: list[CodeSuggestion]
    documentation_gaps: list[str]
    determination: str
    denial_risk_score: float
    determination_rationale: str
    model_coverage_rationale: str
    model_name: str
    model_version: str
    prompt_template_version: str
    generated_at: datetime.datetime
    clause_results: list[ClauseResultView] = field(default_factory=list)
    verification_rejections: list[str] = field(default_factory=list)


def view_from_outcome(
    request: ValidationRequest,
    outcome: ValidationOutcome,
    generated_at: datetime.datetime,
) -> RecommendationView:
    """Map a verified pipeline outcome onto the display contract.

    Pure translation, no reasoning: everything shown was already verified by
    the grounding gates or computed by the clause evaluator, so this function
    must not add, drop, or reword any claim.
    """
    extracted_facts: list[str] = []
    for fact in outcome.verified.extracted_facts:
        extracted_facts.append(fact.fact)

    code_suggestions: list[CodeSuggestion] = []
    for recommendation in outcome.verified.code_recommendations:
        spans: list[NoteSpan] = []
        for located in recommendation.supporting_spans:
            spans.append(
                NoteSpan(
                    text=located.text,
                    start_offset=located.start_offset,
                    end_offset=located.end_offset,
                )
            )
        code_suggestions.append(
            CodeSuggestion(
                code=recommendation.code,
                code_system=recommendation.code_system,
                description=recommendation.description,
                rationale=recommendation.rationale,
                supporting_note_spans=spans,
            )
        )

    clause_results: list[ClauseResultView] = []
    for clause_result in outcome.assessment.clause_results:
        evidence: list[NoteSpan] = []
        for located in clause_result.evidence:
            evidence.append(
                NoteSpan(
                    text=located.text,
                    start_offset=located.start_offset,
                    end_offset=located.end_offset,
                )
            )
        clause_results.append(
            ClauseResultView(
                policy_identifier=clause_result.policy_identifier,
                clause_id=clause_result.clause_id,
                title=clause_result.title,
                status=clause_result.status,
                decided_by=clause_result.decided_by,
                detail=clause_result.detail,
                required=clause_result.required,
                evidence=evidence,
            )
        )

    return RecommendationView(
        is_sample=False,
        input_reference=request.input_reference,
        requested_service=request.requested_service,
        date_of_service=request.date_of_service,
        payer_name=request.payer_name,
        extracted_facts=extracted_facts,
        code_suggestions=code_suggestions,
        documentation_gaps=outcome.verified.documentation_gaps,
        determination=outcome.assessment.determination,
        denial_risk_score=outcome.assessment.denial_risk_score,
        determination_rationale=outcome.assessment.determination_rationale,
        model_coverage_rationale=outcome.verified.coverage_rationale,
        model_name=outcome.model_name,
        model_version=outcome.model_name,
        prompt_template_version=outcome.prompt_template_version,
        generated_at=generated_at,
        clause_results=clause_results,
        verification_rejections=outcome.verified.rejections,
    )


def _find_span(note_text: str, phrase: str) -> NoteSpan:
    """Locate a phrase in the note, returning real offsets when found.

    A located span shows the UI can cite the exact supporting text (guardrail
    4). When the phrase is absent (for example the user pasted a different
    note), the span is returned unlocated so the UI can mark it illustrative
    rather than fabricate an offset.
    """
    start_offset = note_text.find(phrase)
    if start_offset == -1:
        return NoteSpan(text=phrase, start_offset=None, end_offset=None)
    end_offset = start_offset + len(phrase)
    return NoteSpan(text=phrase, start_offset=start_offset, end_offset=end_offset)


def build_sample_recommendation(
    note_text: str,
    requested_service: str,
    date_of_service: datetime.date,
    payer_name: str,
    generated_at: datetime.datetime,
) -> RecommendationView:
    """Build a fixed, clearly-labeled SAMPLE recommendation for the review UI.

    Shown only when configuration (API key or database) is missing, so the
    surface is still demonstrable. It performs no clinical reasoning. Note
    spans are located in the supplied note when the sample phrases happen to
    be present, purely to demonstrate the citation UI.
    """
    supporting_spans = []
    supporting_spans.append(
        _find_span(note_text, "Low back pain radiating to left leg, 8 weeks duration")
    )
    supporting_spans.append(
        _find_span(note_text, "Diminished sensation in the left L5 dermatome")
    )

    code_suggestion = CodeSuggestion(
        code="M54.16",
        code_system="ICD-10-CM",
        description="Radiculopathy, lumbar region",
        rationale=(
            "Most specific supported code for documented lumbar radiculopathy. "
            "Chosen for specificity and accuracy, not payment: a less specific "
            "back-pain code would be supported but would not reflect the "
            "documented radicular findings."
        ),
        supporting_note_spans=supporting_spans,
    )

    clause_results = [
        ClauseResultView(
            policy_identifier="SYN-LUMBAR-MRI-001",
            clause_id="symptom_duration",
            title="Symptom duration and character",
            status="satisfied",
            decided_by="rule+model",
            detail=(
                "SAMPLE: rule min_duration: documented 8 weeks >= threshold "
                "6 weeks; model judgment: satisfied (1 evidence span(s))"
            ),
            required=True,
            evidence=[
                _find_span(
                    note_text,
                    "Low back pain radiating to left leg, 8 weeks duration",
                )
            ],
        ),
        ClauseResultView(
            policy_identifier="SYN-LUMBAR-MRI-001",
            clause_id="objective_findings",
            title="Objective neurologic findings",
            status="satisfied",
            decided_by="model",
            detail="SAMPLE: model judgment: satisfied (1 evidence span(s))",
            required=True,
            evidence=[
                _find_span(
                    note_text, "Diminished sensation in the left L5 dermatome"
                )
            ],
        ),
        ClauseResultView(
            policy_identifier="SYN-LUMBAR-MRI-001",
            clause_id="not_recent_duplicate",
            title="No recent duplicate study",
            status="insufficient_documentation",
            decided_by="model",
            detail=(
                "SAMPLE: no verified model judgment for this clause; silence "
                "fails closed"
            ),
            required=True,
        ),
    ]

    extracted_facts = [
        "Low back pain with left leg radiation, documented duration 8 weeks.",
        "Completed 6 weeks of physical therapy and a trial of NSAIDs.",
        "Positive straight-leg-raise on the left and diminished left L5 sensation.",
        "No documented red-flag findings (denies saddle anesthesia, bowel or "
        "bladder dysfunction, fever).",
    ]

    documentation_gaps = [
        "If clinically accurate, document the specific functional limitation "
        "caused by the radicular symptoms to strengthen medical necessity.",
        "If clinically accurate, note whether any prior lumbar imaging has been "
        "performed for this episode and its result.",
    ]

    recommendation = RecommendationView(
        is_sample=True,
        input_reference="SAMPLE-note-ref",
        requested_service=requested_service,
        date_of_service=date_of_service,
        payer_name=payer_name,
        extracted_facts=extracted_facts,
        code_suggestions=[code_suggestion],
        documentation_gaps=documentation_gaps,
        determination="insufficient_documentation",
        denial_risk_score=0.425,
        determination_rationale=(
            "SAMPLE. Computed from clause statuses "
            "(symptom_duration=satisfied; objective_findings=satisfied; "
            "not_recent_duplicate=insufficient_documentation). Determination: "
            "insufficient_documentation."
        ),
        model_coverage_rationale=(
            "SAMPLE narrative: the documentation supports duration, therapy, "
            "and objective findings, but does not address prior imaging."
        ),
        model_name="SAMPLE (not analyzed)",
        model_version="none",
        prompt_template_version="none",
        generated_at=generated_at,
        clause_results=clause_results,
    )
    return recommendation

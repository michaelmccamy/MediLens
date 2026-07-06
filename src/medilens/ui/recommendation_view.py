"""Display contract for a coding recommendation, plus a labeled sample.

This is the shape the review UI renders. It mirrors the fields the audit store
persists (see medilens.audit.writer.RecommendationRecord and the Recommendation
model): extracted facts, recommended codes, cited note spans, cited policy
clauses, denial-risk score, and model/prompt provenance. Building the UI against
this contract means the real reasoning layer, once it exists, populates the same
structure with no UI changes.

IMPORTANT: build_sample_recommendation returns a fixed, illustrative SAMPLE. The
reasoning layer is not implemented yet, so nothing here analyzes the note. Every
field is marked is_sample=True and the model/prompt versions say so, and the UI
labels the output as a sample. This matters in a clinical tool: unlabeled
placeholder output that looks like a real analysis would be dangerous.

No streamlit import here on purpose, so the contract and the sample are plain
Python and unit-testable without the UI.
"""

import datetime
from dataclasses import dataclass, field


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
class PolicyClauseCitation:
    """A specific payer-policy clause cited as the coverage basis."""

    policy_identifier: str
    clause_number: int
    clause_text: str


@dataclass
class CodeSuggestion:
    """One recommended code with its grounding.

    rationale states why this is the most accurate supported code, never the
    highest paying one (CLAUDE.md guiding principle). supporting_note_spans and
    cited_policy_clauses satisfy the grounding-and-provenance rule (guardrail 4).
    """

    code: str
    code_system: str
    description: str
    rationale: str
    supporting_note_spans: list[NoteSpan] = field(default_factory=list)
    cited_policy_clauses: list[PolicyClauseCitation] = field(default_factory=list)


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
    denial_risk_score: float
    denial_risk_rationale: str
    model_name: str
    model_version: str
    prompt_template_version: str
    generated_at: datetime.datetime


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

    This is the seam where the reasoning layer will plug in: replace this call
    with the real extract-match-explain pipeline, returning the same
    RecommendationView. Until then it returns an illustrative example anchored
    to the lumbar-radiculopathy synthetic note, echoing the request inputs.

    It performs no clinical reasoning. Note spans are located in the supplied
    note when the sample phrases happen to be present, purely to demonstrate the
    citation UI.
    """
    supporting_spans = []
    supporting_spans.append(
        _find_span(note_text, "Low back pain radiating to left leg, 8 weeks duration")
    )
    supporting_spans.append(
        _find_span(note_text, "Diminished sensation in the left L5 dermatome")
    )

    mri_policy_clause = PolicyClauseCitation(
        policy_identifier="SYN-LUMBAR-MRI-001",
        clause_number=3,
        clause_text=(
            "Documentation records objective neurologic findings on examination "
            "(for example a positive straight-leg-raise, dermatomal sensory loss, "
            "motor weakness, or reflex change) supporting radiculopathy, or "
            "explains their absence."
        ),
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
        cited_policy_clauses=[mri_policy_clause],
    )

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
        denial_risk_score=0.18,
        denial_risk_rationale=(
            "Low illustrative risk: the documented duration, completed "
            "conservative therapy, and objective neurologic findings align with "
            "the sample coverage criteria. This score is a placeholder, not a "
            "computed prediction."
        ),
        model_name="SAMPLE (reasoning layer not implemented)",
        model_version="none",
        prompt_template_version="none",
        generated_at=generated_at,
    )
    return recommendation

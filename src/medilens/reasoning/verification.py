"""Code-side verification of model validation output (policy schema v2).

The prompt instructs the model to ground everything, but instructions are not
guarantees. This module re-checks every claim mechanically before anything is
shown to a coder, evaluated, or persisted, enforcing CLAUDE.md guardrails and
the policy-v2 verifier rules (docs/policy-schema.md section 10) in code:

- Guardrail 1 (no fabricated facts): every cited span, every fact evidence
  string, and every judgment evidence string must appear verbatim in the note.
  A span that does not locate is dropped and treated as absent.
- Guardrail 4 (grounding): every recommended code must be in the date-resolved
  candidate set and carry at least one located note span.
- No satisfied without evidence: a clause judgment asserting satisfied,
  not_satisfied, or contradictory_documentation with zero verifiable evidence
  spans is DOWNGRADED to insufficient_documentation. The model cannot talk a
  clause into passing without proof.
- Facts verify or drop: a clinical fact whose evidence does not locate, whose
  key was not requested, or whose value does not parse as its declared type is
  dropped and treated as absent, which triggers the fail-closed rule path.
- The model is not asked for statuses on deterministic clauses; if it supplies
  a judgment for one (or for an unknown clause), the judgment is ignored with
  a recorded reason.

Verification is per-item: each dropped or downgraded item is recorded in the
rejections list and the grounded remainder survives. Nothing ungrounded is
ever emitted, and every drop is surfaced to the reviewer and the audit record.

Rejection reasons name the offending key, clause, or code but never include
note content, since they end up in logs and the audit store (guardrail 6).
"""

import datetime
from dataclasses import dataclass
from typing import Any

from medilens.db.models import CodeSetEntry, PayerPolicy
from medilens.policy.structure import FACT_SOURCE_NOTE, PolicyStructure

# The conditional phrasing guardrail 1 requires of every documentation gap.
_CONDITIONAL_PHRASE = "clinically accurate"

# Statuses the model may assert for a judgment (mirrors the schema enum).
_ASSERTABLE_STATUSES = frozenset(
    {
        "satisfied",
        "not_satisfied",
        "insufficient_documentation",
        "contradictory_documentation",
    }
)

# Statuses that require at least one located evidence span (section 9).
_EVIDENCE_REQUIRED_STATUSES = frozenset(
    {"satisfied", "not_satisfied", "contradictory_documentation"}
)


class GroundingError(Exception):
    """Model output failed a grounding check and must not be used or stored."""


@dataclass(frozen=True)
class LocatedSpan:
    """A model-cited span verified to exist in the note, with real offsets."""

    text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class VerifiedFact:
    fact: str
    span: LocatedSpan


@dataclass(frozen=True)
class VerifiedClinicalFact:
    """A typed clinical fact with verified evidence, ready for the rule engine."""

    key: str
    value: Any  # float for duration/count, bool for boolean, date for date
    unit: str | None
    evidence: LocatedSpan


@dataclass(frozen=True)
class VerifiedClauseJudgment:
    """A model judgment that passed the evidence requirements."""

    policy_identifier: str
    clause_id: str
    status: str
    evidence: tuple[LocatedSpan, ...]


@dataclass(frozen=True)
class VerifiedCodeRecommendation:
    """One code recommendation with verified documentation support.

    Coverage is policy-level under schema v2 (clause statuses and a computed
    determination), so codes no longer carry per-code clause citations.
    """

    code: str
    code_system: str
    description: str
    rationale: str
    supporting_spans: list[LocatedSpan]


@dataclass(frozen=True)
class VerifiedValidation:
    """The verified model output: safe to display, evaluate, and persist.

    rejections records every item dropped or downgraded during verification,
    with a PHI-free reason, so a reviewer and the audit trail can see what the
    model produced that did not survive.
    """

    extracted_facts: list[VerifiedFact]
    clinical_facts: dict[str, VerifiedClinicalFact]
    clause_judgments: dict[tuple[str, str], VerifiedClauseJudgment]
    code_recommendations: list[VerifiedCodeRecommendation]
    documentation_gaps: list[str]
    coverage_rationale: str
    rejections: list[str]


def _normalize_with_offset_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to single spaces, keeping original offsets.

    Returns the normalized text and a map from each normalized character back
    to its original index, so a match in normalized space can be translated to
    exact offsets in the original note.
    """
    normalized_chars: list[str] = []
    offset_map: list[int] = []
    previous_was_space = False
    for index, character in enumerate(text):
        if character.isspace():
            if previous_was_space:
                continue
            normalized_chars.append(" ")
            offset_map.append(index)
            previous_was_space = True
        else:
            normalized_chars.append(character)
            offset_map.append(index)
            previous_was_space = False
    return "".join(normalized_chars), offset_map


def _try_locate_span(note_text: str, span_text: str) -> LocatedSpan | None:
    """Find a cited span in the note, or return None if it does not appear.

    Content characters must match exactly; a paraphrased or altered span
    cannot be traced back to the record, which defeats provenance. The one
    tolerated difference is whitespace: clinical notes are often hard-wrapped
    mid-sentence, so a model citing a wrapped sentence renders the line break
    as a space. Whitespace runs are treated as equivalent, which is not a
    fabrication loophole because every non-whitespace character still has to
    match. The returned span text is taken from the note itself (not the model
    output), so what is persisted is exactly what the record says.

    Returns None rather than raising, so the caller can drop just the offending
    citation and record a reason.
    """
    if not span_text or span_text.isspace():
        return None

    # Fast path: the span appears character for character.
    start_offset = note_text.find(span_text)
    if start_offset != -1:
        end_offset = start_offset + len(span_text)
        return LocatedSpan(
            text=span_text, start_offset=start_offset, end_offset=end_offset
        )

    # Whitespace-tolerant path: match with whitespace runs collapsed, then map
    # back to exact offsets in the original note.
    normalized_note, offset_map = _normalize_with_offset_map(note_text)
    normalized_span, _ = _normalize_with_offset_map(span_text.strip())
    if not normalized_span:
        return None
    normalized_start = normalized_note.find(normalized_span)
    if normalized_start == -1:
        return None
    normalized_end = normalized_start + len(normalized_span) - 1
    start_offset = offset_map[normalized_start]
    end_offset = offset_map[normalized_end] + 1
    return LocatedSpan(
        text=note_text[start_offset:end_offset],
        start_offset=start_offset,
        end_offset=end_offset,
    )


def _parse_fact_value(raw_value: str, fact_type: str) -> Any | None:
    """Parse a fact value string as its declared type, or None if it does not.

    The model is instructed to return plain value strings; anything that does
    not parse is treated as absent (fail closed), never coerced creatively.
    """
    text = str(raw_value).strip()
    if fact_type in ("duration", "count"):
        # Tolerate a trailing percent sign on counts expressed as percentages.
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return float(text)
        except ValueError:
            return None
    if fact_type == "boolean":
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return None
    if fact_type == "date":
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            return None
    return None


def verify_validation_output(
    output: Any,
    note_text: str,
    candidate_codes: list[CodeSetEntry],
    policies: list[tuple[PayerPolicy, PolicyStructure]],
) -> VerifiedValidation:
    """Re-check every claim in the model output against ground truth.

    output is the parsed JSON from ModelClient.create_structured. The schema
    guarantees its shape; this function checks its truth. Returns the verified
    result; dropped and downgraded items are listed in rejections.
    """
    candidate_by_code: dict[str, CodeSetEntry] = {}
    for entry in candidate_codes:
        candidate_by_code[entry.code] = entry

    # Combined note-fact specs and judgment-bearing clauses across the
    # matched policies. Fact keys must not conflict across policies.
    note_fact_specs: dict[str, Any] = {}
    for _policy_row, structure in policies:
        for fact_spec in structure.required_facts:
            if fact_spec.source != FACT_SOURCE_NOTE:
                continue
            existing = note_fact_specs.get(fact_spec.key)
            if existing is not None and existing.type != fact_spec.type:
                raise GroundingError(
                    f"fact key {fact_spec.key!r} is declared with conflicting "
                    "types by two matched policies; policies cannot be "
                    "evaluated together"
                )
            note_fact_specs[fact_spec.key] = fact_spec

    judgment_clauses: dict[tuple[str, str], Any] = {}
    for policy_row, structure in policies:
        for clause in structure.clauses:
            if clause.needs_judgment:
                judgment_clauses[(policy_row.policy_identifier, clause.clause_id)] = (
                    clause
                )

    rejections: list[str] = []

    verified_facts: list[VerifiedFact] = []
    for fact_item in output["extracted_facts"]:
        span = _try_locate_span(note_text, fact_item["note_span"])
        if span is None:
            rejections.append(
                "dropped an extracted fact: its cited note span was not found "
                "in the note (possible fabrication or paraphrase, guardrail 1)"
            )
            continue
        verified_facts.append(VerifiedFact(fact=fact_item["fact"], span=span))

    # Clinical facts: verify key, type, and evidence; drop anything unproven.
    clinical_facts: dict[str, VerifiedClinicalFact] = {}
    for fact_item in output["clinical_facts"]:
        key = fact_item["key"]
        spec = note_fact_specs.get(key)
        if spec is None:
            rejections.append(
                f"ignored clinical fact {key}: not a requested note-sourced "
                "fact for any matched policy (the model must not invent or "
                "supply history-sourced values)"
            )
            continue
        if key in clinical_facts:
            rejections.append(
                f"ignored a duplicate clinical fact {key}: first verified "
                "value wins"
            )
            continue
        value = _parse_fact_value(fact_item["value"], spec.type)
        if value is None:
            rejections.append(
                f"dropped clinical fact {key}: value did not parse as "
                f"{spec.type}; treated as undocumented (fail closed)"
            )
            continue
        evidence = _try_locate_span(note_text, fact_item["evidence"])
        if evidence is None:
            rejections.append(
                f"dropped clinical fact {key}: its evidence was not found "
                "verbatim in the note; treated as undocumented (fail closed, "
                "guardrail 1)"
            )
            continue
        clinical_facts[key] = VerifiedClinicalFact(
            key=key, value=value, unit=spec.unit, evidence=evidence
        )

    # Clause judgments: verify the clause exists and takes a judgment, locate
    # evidence, and enforce the no-satisfied-without-evidence rule.
    clause_judgments: dict[tuple[str, str], VerifiedClauseJudgment] = {}
    for judgment_item in output["clause_judgments"]:
        judgment_key = (
            judgment_item["policy_identifier"],
            judgment_item["clause_id"],
        )
        clause_label = f"{judgment_key[0]}.{judgment_key[1]}"
        if judgment_key not in judgment_clauses:
            rejections.append(
                f"ignored a judgment for {clause_label}: not a "
                "judgment-bearing clause of any matched policy (deterministic "
                "and manual-review clauses are decided in code)"
            )
            continue
        if judgment_key in clause_judgments:
            rejections.append(
                f"ignored a duplicate judgment for {clause_label}: first "
                "verified judgment wins"
            )
            continue
        status = judgment_item["status"]
        if status not in _ASSERTABLE_STATUSES:
            # The schema enum should prevent this; fail closed if it appears.
            rejections.append(
                f"downgraded judgment for {clause_label}: status {status!r} "
                "is not assertable by the model"
            )
            status = "insufficient_documentation"

        located_evidence: list[LocatedSpan] = []
        for evidence_text in judgment_item["evidence"]:
            located = _try_locate_span(note_text, evidence_text)
            if located is None:
                rejections.append(
                    f"dropped an evidence span for {clause_label}: not found "
                    "verbatim in the note (guardrail 1)"
                )
                continue
            located_evidence.append(located)

        if status in _EVIDENCE_REQUIRED_STATUSES and len(located_evidence) == 0:
            rejections.append(
                f"downgraded judgment for {clause_label}: {status} requires "
                "verified evidence and none survived; recorded as "
                "insufficient_documentation (no satisfied without evidence)"
            )
            status = "insufficient_documentation"

        clause_judgments[judgment_key] = VerifiedClauseJudgment(
            policy_identifier=judgment_key[0],
            clause_id=judgment_key[1],
            status=status,
            evidence=tuple(located_evidence),
        )

    verified_recommendations: list[VerifiedCodeRecommendation] = []
    for recommendation in output["code_recommendations"]:
        code = recommendation["code"]

        candidate = candidate_by_code.get(code)
        if candidate is None:
            rejections.append(
                f"dropped code {code}: not in the date-resolved candidate set "
                "(no freeform code guessing, guardrail 4)"
            )
            continue
        if recommendation["code_system"] != candidate.code_system:
            rejections.append(
                f"dropped code {code}: code_system did not match the candidate set"
            )
            continue

        supporting_spans: list[LocatedSpan] = []
        for span_text in recommendation["supporting_note_spans"]:
            located = _try_locate_span(note_text, span_text)
            if located is None:
                rejections.append(
                    f"dropped a supporting span for code {code}: not found in "
                    "the note (guardrail 1)"
                )
                continue
            supporting_spans.append(located)
        if len(supporting_spans) == 0:
            rejections.append(
                f"dropped code {code}: no supporting note span could be located, "
                "so it has no documentation support (guardrails 2 and 4)"
            )
            continue

        verified_recommendations.append(
            VerifiedCodeRecommendation(
                code=code,
                code_system=candidate.code_system,
                description=candidate.description,
                rationale=recommendation["rationale"],
                supporting_spans=supporting_spans,
            )
        )

    documentation_gaps: list[str] = []
    for gap in output["documentation_gaps"]:
        if _CONDITIONAL_PHRASE not in gap.lower():
            rejections.append(
                "dropped a documentation gap: not phrased conditionally on "
                "clinical accuracy (guardrail 1)"
            )
            continue
        documentation_gaps.append(gap)

    return VerifiedValidation(
        extracted_facts=verified_facts,
        clinical_facts=clinical_facts,
        clause_judgments=clause_judgments,
        code_recommendations=verified_recommendations,
        documentation_gaps=documentation_gaps,
        coverage_rationale=output["coverage_rationale"],
        rejections=rejections,
    )

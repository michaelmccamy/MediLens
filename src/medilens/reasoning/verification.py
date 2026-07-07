"""Code-side grounding verification of model validation output.

The prompt instructs the model to ground everything, but instructions are not
guarantees. This module re-checks every claim mechanically before anything is
shown to a coder or persisted, enforcing CLAUDE.md guardrails in code:

- Guardrail 1 (no fabricated facts): every extracted fact's note span and every
  supporting span must appear verbatim in the note. A span that does not locate
  is treated as fabrication and rejects the whole output.
- Guardrail 4 (grounding and provenance): every recommended code must be in the
  date-resolved candidate set (no freeform code guessing), must carry at least
  one located note span, and every cited policy clause must exist in the
  retrieved policies.
- Guardrail 2 (no upcoding): the enforceable proxy today is that a code without
  located documentation support is rejected outright. Payment-aware ranking
  needs fee schedule data the MVP does not have; when that lands, the check
  belongs here.
- Guardrail 3 (human in the loop) via guardrail 1 phrasing: documentation gaps
  must be conditional on clinical accuracy.

Everything fails loudly with GroundingError and nothing partial survives
(CLAUDE.md section 7). Error messages identify the failing field but never
include note content, since exceptions end up in logs (guardrail 6).
"""

import re
from dataclasses import dataclass
from typing import Any

from medilens.db.models import CodeSetEntry, PayerPolicy

# Matches the numbered clause lines produced by policy.ingest.render_policy_text.
_CLAUSE_LINE_PATTERN = re.compile(r"^(\d+)\.\s+(.*)$")

# The conditional phrasing guardrail 1 requires of every documentation gap.
_CONDITIONAL_PHRASE = "clinically accurate"


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
class VerifiedClauseCitation:
    policy_identifier: str
    clause_number: int
    clause_text: str


@dataclass(frozen=True)
class VerifiedCodeRecommendation:
    code: str
    code_system: str
    description: str
    rationale: str
    supporting_spans: list[LocatedSpan]
    cited_clauses: list[VerifiedClauseCitation]


@dataclass(frozen=True)
class VerifiedValidation:
    """The fully verified output: safe to display and persist."""

    extracted_facts: list[VerifiedFact]
    code_recommendations: list[VerifiedCodeRecommendation]
    documentation_gaps: list[str]
    denial_risk_score: float
    denial_risk_rationale: str


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


def _locate_span(note_text: str, span_text: str, field_name: str) -> LocatedSpan:
    """Find a cited span in the note or reject the output.

    Content characters must match exactly; a paraphrased or altered span
    cannot be traced back to the record, which defeats provenance. The one
    tolerated difference is whitespace: clinical notes are often hard-wrapped
    mid-sentence, so a model citing a wrapped sentence renders the line break
    as a space. Whitespace runs are treated as equivalent, which is not a
    fabrication loophole because every non-whitespace character still has to
    match. The stored span text is taken from the note itself (not the model
    output), so what is persisted is exactly what the record says.

    The error names the field but not the span content (no note text in logs,
    guardrail 6).
    """
    if not span_text or span_text.isspace():
        raise GroundingError(f"{field_name} is empty; every citation needs text")

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
    normalized_start = normalized_note.find(normalized_span)
    if normalized_start == -1:
        raise GroundingError(
            f"{field_name} does not appear verbatim in the note; treating as "
            "fabrication and rejecting the output (guardrail 1)"
        )
    normalized_end = normalized_start + len(normalized_span) - 1
    start_offset = offset_map[normalized_start]
    end_offset = offset_map[normalized_end] + 1
    return LocatedSpan(
        text=note_text[start_offset:end_offset],
        start_offset=start_offset,
        end_offset=end_offset,
    )


def _build_clause_lookup(
    policies: list[PayerPolicy],
) -> dict[str, dict[int, str]]:
    """Map policy_identifier to {clause_number: clause_text} from policy_text.

    Clause numbers are parsed from the numbered lines render_policy_text
    produces. When multiple in-force versions of the same identifier exist,
    the most recently ingested one wins, matching what a coder would be shown.
    """
    ordered_policies = sorted(policies, key=lambda policy: policy.retrieved_at)
    lookup: dict[str, dict[int, str]] = {}
    for policy in ordered_policies:
        clauses: dict[int, str] = {}
        for line in policy.policy_text.splitlines():
            match = _CLAUSE_LINE_PATTERN.match(line.strip())
            if match is not None:
                clause_number = int(match.group(1))
                clauses[clause_number] = match.group(2)
        lookup[policy.policy_identifier] = clauses
    return lookup


def verify_validation_output(
    output: Any,
    note_text: str,
    candidate_codes: list[CodeSetEntry],
    policies: list[PayerPolicy],
) -> VerifiedValidation:
    """Re-check every claim in the model output against ground truth.

    output is the parsed JSON from ModelClient.create_structured. The schema
    guarantees its shape; this function checks its truth. Returns the verified
    result or raises GroundingError, in which case nothing may be displayed or
    persisted.
    """
    candidate_by_code: dict[str, CodeSetEntry] = {}
    for entry in candidate_codes:
        candidate_by_code[entry.code] = entry

    clause_lookup = _build_clause_lookup(policies)

    verified_facts: list[VerifiedFact] = []
    for fact_item in output["extracted_facts"]:
        span = _locate_span(
            note_text, fact_item["note_span"], "extracted_facts.note_span"
        )
        verified_facts.append(VerifiedFact(fact=fact_item["fact"], span=span))

    verified_recommendations: list[VerifiedCodeRecommendation] = []
    for recommendation in output["code_recommendations"]:
        code = recommendation["code"]

        candidate = candidate_by_code.get(code)
        if candidate is None:
            raise GroundingError(
                f"recommended code {code!r} is not in the date-resolved "
                "candidate set; no freeform code guessing (guardrail 4)"
            )
        if recommendation["code_system"] != candidate.code_system:
            raise GroundingError(
                f"recommended code {code!r} carries code_system "
                f"{recommendation['code_system']!r} but the candidate set has "
                f"{candidate.code_system!r}"
            )

        supporting_spans: list[LocatedSpan] = []
        for span_text in recommendation["supporting_note_spans"]:
            located = _locate_span(
                note_text, span_text, f"code {code} supporting_note_spans"
            )
            supporting_spans.append(located)
        if len(supporting_spans) == 0:
            raise GroundingError(
                f"recommended code {code!r} has no supporting note spans; a "
                "code without documentation support is rejected (guardrails 2 "
                "and 4)"
            )

        cited_clauses: list[VerifiedClauseCitation] = []
        for clause_citation in recommendation["cited_policy_clauses"]:
            policy_identifier = clause_citation["policy_identifier"]
            clause_number = clause_citation["clause_number"]
            policy_clauses = clause_lookup.get(policy_identifier)
            if policy_clauses is None:
                raise GroundingError(
                    f"cited policy {policy_identifier!r} is not in the "
                    "date-resolved policy set for this payer (guardrail 4)"
                )
            clause_text = policy_clauses.get(clause_number)
            if clause_text is None:
                raise GroundingError(
                    f"cited clause {clause_number} does not exist in policy "
                    f"{policy_identifier!r} (guardrail 4)"
                )
            cited_clauses.append(
                VerifiedClauseCitation(
                    policy_identifier=policy_identifier,
                    clause_number=clause_number,
                    clause_text=clause_text,
                )
            )
        if len(cited_clauses) == 0:
            raise GroundingError(
                f"recommended code {code!r} cites no policy clauses; every "
                "recommendation needs its coverage basis (guardrail 4)"
            )

        verified_recommendations.append(
            VerifiedCodeRecommendation(
                code=code,
                code_system=candidate.code_system,
                description=candidate.description,
                rationale=recommendation["rationale"],
                supporting_spans=supporting_spans,
                cited_clauses=cited_clauses,
            )
        )

    documentation_gaps: list[str] = []
    for gap in output["documentation_gaps"]:
        if _CONDITIONAL_PHRASE not in gap.lower():
            raise GroundingError(
                "documentation gap is not phrased conditionally on clinical "
                "accuracy (guardrail 1)"
            )
        documentation_gaps.append(gap)

    denial_risk_score = output["denial_risk_score"]
    if not 0.0 <= denial_risk_score <= 1.0:
        raise GroundingError(
            f"denial_risk_score {denial_risk_score} is outside [0.0, 1.0]"
        )

    return VerifiedValidation(
        extracted_facts=verified_facts,
        code_recommendations=verified_recommendations,
        documentation_gaps=documentation_gaps,
        denial_risk_score=float(denial_risk_score),
        denial_risk_rationale=output["denial_risk_rationale"],
    )

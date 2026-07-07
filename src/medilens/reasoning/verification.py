"""Code-side grounding verification of model validation output.

The prompt instructs the model to ground everything, but instructions are not
guarantees. This module re-checks every claim mechanically before anything is
shown to a coder or persisted, enforcing CLAUDE.md guardrails in code:

- Guardrail 1 (no fabricated facts): every extracted fact's note span and every
  supporting span must appear verbatim in the note. A span that does not locate
  is treated as fabrication.
- Guardrail 4 (grounding and provenance): every recommended code must be in the
  date-resolved candidate set (no freeform code guessing) and must carry at
  least one located note span. Cited policy clauses must exist in the retrieved
  policies; invalid citations are dropped. Documentation support and coverage
  basis are decoupled: a supported code whose clause citations all fail
  verification survives with has_coverage_basis False, an explicit state every
  surface renders, rather than being discarded or silently implying coverage.
- Guardrail 2 (no upcoding): the enforceable proxy today is that a code without
  located documentation support is dropped. Payment-aware ranking needs fee
  schedule data the MVP does not have; when that lands, the check belongs here.
- Guardrail 3 (human in the loop) via guardrail 1 phrasing: documentation gaps
  must be conditional on clinical accuracy.

Verification is per-item, not all-or-nothing. A single fact, code, span, clause,
or gap that fails its check is DROPPED, and the reason is recorded in the
returned rejections list; the grounded remainder survives. This is not silent
guessing (CLAUDE.md section 7): every drop is surfaced to the reviewer and
written to the audit record, and nothing ungrounded is ever emitted. If all
codes are dropped, the outcome is an honest "no supported codes" plus the
reasons, which is a valid finding (guardrail 4).

One structural failure is still a hard stop: a denial_risk_score outside the
0.0 to 1.0 scale means the model did not follow the schema semantics, which
casts doubt on the assessment as a whole, so it raises GroundingError.

Rejection reasons name the offending code or field but never include note
content, since they end up in logs and the audit store (guardrail 6).
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
    """One recommendation with verified documentation support.

    Documentation support (located note spans) and coverage basis (verified
    policy clauses) are separate questions. A code with spans but no valid
    clause is still a useful, honest finding: "supported by the note; no
    coverage basis cited". has_coverage_basis makes that state explicit so
    every surface (UI, CLI, audit record) must confront it rather than
    implying coverage.
    """

    code: str
    code_system: str
    description: str
    rationale: str
    supporting_spans: list[LocatedSpan]
    cited_clauses: list[VerifiedClauseCitation]

    @property
    def has_coverage_basis(self) -> bool:
        return len(self.cited_clauses) > 0


@dataclass(frozen=True)
class VerifiedValidation:
    """The verified output: safe to display and persist.

    rejections records every item dropped during verification, with a
    PHI-free reason, so a reviewer and the audit trail can see what the model
    produced that did not survive grounding.
    """

    extracted_facts: list[VerifiedFact]
    code_recommendations: list[VerifiedCodeRecommendation]
    documentation_gaps: list[str]
    denial_risk_score: float
    denial_risk_rationale: str
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

        cited_clauses: list[VerifiedClauseCitation] = []
        for clause_citation in recommendation["cited_policy_clauses"]:
            policy_identifier = clause_citation["policy_identifier"]
            clause_number = clause_citation["clause_number"]
            policy_clauses = clause_lookup.get(policy_identifier)
            if policy_clauses is None:
                rejections.append(
                    f"dropped a citation for code {code}: policy "
                    f"{policy_identifier} is not in the retrieved policy set "
                    "(guardrail 4)"
                )
                continue
            clause_text = policy_clauses.get(clause_number)
            if clause_text is None:
                rejections.append(
                    f"dropped a citation for code {code}: clause {clause_number} "
                    f"does not exist in policy {policy_identifier} (guardrail 4)"
                )
                continue
            cited_clauses.append(
                VerifiedClauseCitation(
                    policy_identifier=policy_identifier,
                    clause_number=clause_number,
                    clause_text=clause_text,
                )
            )
        # Documentation support and coverage basis are decoupled: a code with
        # located spans but no valid clause survives, explicitly flagged via
        # has_coverage_basis, instead of being discarded. The reviewer decides
        # what a supported-but-uncovered code means for the claim.
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
            rejections.append(
                "dropped a documentation gap: not phrased conditionally on "
                "clinical accuracy (guardrail 1)"
            )
            continue
        documentation_gaps.append(gap)

    # Structural hard stop: an out-of-scale risk score means the model did not
    # follow the schema semantics, so the whole assessment is untrustworthy.
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
        rejections=rejections,
    )

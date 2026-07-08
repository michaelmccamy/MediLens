"""JSON schema for the structured validation output (prompt v3 contract).

Passed to ModelClient.create_structured, which enforces it server-side via
structured outputs, so the response shape is guaranteed before parsing.

Under policy schema v2 the model never emits an overall determination or a
denial-risk score: those are computed in code from clause statuses
(docs/policy-schema.md sections 7 and 11). The model returns:

- extracted_facts: display facts with verbatim spans (unchanged from v1/v2).
- clinical_facts: typed values for the facts each policy's deterministic
  rules consume, each with verbatim evidence. Facts the note does not state
  are OMITTED, never guessed; a missing fact is the fail-closed trigger.
- clause_judgments: a status per judgment-bearing clause, with verbatim
  evidence. The model may only assert the four statuses below; not_applicable
  and manual_review are decided in code.
- code_recommendations: codes from the candidate set with supporting spans.
  Per-code clause citations are gone; coverage is policy-level now.
- documentation_gaps and coverage_rationale (prose only; no scores).

Shape guarantees are not truth guarantees: everything semantic (spans really
in the note, codes really in the candidate set, clause ids really in the
policy, values really parseable as their declared type) is checked in
verification.py.
"""

from typing import Any

# The four statuses the model may assert for a judgment-bearing clause.
MODEL_ASSERTABLE_STATUSES = [
    "satisfied",
    "not_satisfied",
    "insufficient_documentation",
    "contradictory_documentation",
]

VALIDATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "extracted_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "One clinical fact stated in the note.",
                    },
                    "note_span": {
                        "type": "string",
                        "description": (
                            "Exact verbatim substring of the note that states "
                            "this fact."
                        ),
                    },
                },
                "required": ["fact", "note_span"],
                "additionalProperties": False,
            },
        },
        "clinical_facts": {
            "type": "array",
            "description": (
                "Typed values for the requested FACTS TO EXTRACT keys. Omit "
                "any fact the note does not explicitly state; never guess."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "A key from FACTS TO EXTRACT, exactly as written.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "The documented value as a plain string: a number "
                            "for durations and counts (for example '8'), "
                            "'true' or 'false' for booleans, YYYY-MM-DD for "
                            "dates."
                        ),
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Exact verbatim substring of the note stating "
                            "this value."
                        ),
                    },
                },
                "required": ["key", "value", "evidence"],
                "additionalProperties": False,
            },
        },
        "clause_judgments": {
            "type": "array",
            "description": (
                "One judgment per clause listed in CLAUSES TO ASSESS. A "
                "clause the note says nothing about gets "
                "insufficient_documentation."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "policy_identifier": {"type": "string"},
                    "clause_id": {
                        "type": "string",
                        "description": "A clause_id from CLAUSES TO ASSESS, exactly as written.",
                    },
                    "status": {
                        "type": "string",
                        "enum": MODEL_ASSERTABLE_STATUSES,
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "description": (
                                "Exact verbatim substring of the note "
                                "supporting this status."
                            ),
                        },
                    },
                },
                "required": ["policy_identifier", "clause_id", "status", "evidence"],
                "additionalProperties": False,
            },
        },
        "code_recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "A code from CANDIDATE CODES, exactly as written.",
                    },
                    "code_system": {"type": "string"},
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Why this is the most specific supported code, "
                            "never payment based."
                        ),
                    },
                    "supporting_note_spans": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "description": (
                                "Exact verbatim substring of the note "
                                "supporting this code."
                            ),
                        },
                    },
                },
                "required": [
                    "code",
                    "code_system",
                    "rationale",
                    "supporting_note_spans",
                ],
                "additionalProperties": False,
            },
        },
        "documentation_gaps": {
            "type": "array",
            "items": {
                "type": "string",
                "description": (
                    "A missing documentation element, phrased conditionally: "
                    "'If clinically accurate, document ...'."
                ),
            },
        },
        "coverage_rationale": {
            "type": "string",
            "description": (
                "Short prose narrative about the documentation relative to "
                "the policy. Never a score and never an overall verdict; the "
                "determination is computed by the system."
            ),
        },
    },
    "required": [
        "extracted_facts",
        "clinical_facts",
        "clause_judgments",
        "code_recommendations",
        "documentation_gaps",
        "coverage_rationale",
    ],
    "additionalProperties": False,
}

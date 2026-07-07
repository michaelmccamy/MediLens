"""JSON schema for the structured validation output.

Passed to ModelClient.create_structured, which enforces it server-side via
structured outputs, so the response shape is guaranteed before parsing.
Structured outputs do not support numeric range constraints, so the 0.0 to 1.0
bound on denial_risk_score is enforced in verification.py instead. Shape
guarantees are not truth guarantees: everything semantic (spans really in the
note, codes really in the candidate set, clauses really in the policy) is
checked in verification.py.
"""

from typing import Any

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
                    "cited_policy_clauses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "policy_identifier": {"type": "string"},
                                "clause_number": {"type": "integer"},
                            },
                            "required": ["policy_identifier", "clause_number"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "code",
                    "code_system",
                    "rationale",
                    "supporting_note_spans",
                    "cited_policy_clauses",
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
        "denial_risk_score": {
            "type": "number",
            "description": "Denial likelihood between 0.0 and 1.0.",
        },
        "denial_risk_rationale": {"type": "string"},
    },
    "required": [
        "extracted_facts",
        "code_recommendations",
        "documentation_gaps",
        "denial_risk_score",
        "denial_risk_rationale",
    ],
    "additionalProperties": False,
}

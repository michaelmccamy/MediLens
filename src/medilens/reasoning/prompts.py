"""Prompt template loading and user-content assembly for the reasoning layer.

CLAUDE.md section 5 requires prompt templates in versioned files, not inline
strings, and requires logging which template version produced each output. A
template is a file named <name>_<version>.txt under src/medilens/prompts/; the
version travels with the loaded template and ends up in the audit record.

The template file is the static system prompt. Everything per-request (note,
metadata, retrieved codes, and the structured policies) is assembled into the
user content by build_user_content, so the system prompt stays byte-identical
across requests, which is also what makes it cacheable later.
"""

import datetime
from dataclasses import dataclass
from pathlib import Path

from medilens.db.models import CodeSetEntry, PayerPolicy
from medilens.policy.structure import FACT_SOURCE_NOTE, PolicyStructure

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_VALIDATION_PROMPT_NAME = "validation"
# v3: the policy-v2 contract. The model extracts typed clinical facts and
# judges clauses by clause_id with cited evidence; it no longer emits a
# denial-risk score or per-code clause citations (both are computed in code).
# Old template files are never edited or deleted, so any audit record's
# prompt_template_version can be reproduced exactly.
DEFAULT_VALIDATION_PROMPT_VERSION = "v3"


@dataclass(frozen=True)
class PromptTemplate:
    """A loaded prompt template plus the version identifier for the audit trail."""

    name: str
    version: str
    text: str


def load_prompt_template(
    name: str = DEFAULT_VALIDATION_PROMPT_NAME,
    version: str = DEFAULT_VALIDATION_PROMPT_VERSION,
) -> PromptTemplate:
    """Load a versioned prompt template, failing loudly if it does not exist.

    A missing template is a configuration error, not something to paper over
    with a default string (CLAUDE.md section 7).
    """
    template_path = _PROMPTS_DIR / f"{name}_{version}.txt"
    if not template_path.exists():
        raise FileNotFoundError(
            f"prompt template not found: {template_path} "
            f"(name={name!r}, version={version!r})"
        )
    text = template_path.read_text(encoding="utf-8")
    return PromptTemplate(name=name, version=version, text=text)


def build_user_content(
    note_text: str,
    requested_service: str,
    date_of_service: datetime.date,
    payer_name: str,
    candidate_codes: list[CodeSetEntry],
    policies: list[tuple[PayerPolicy, PolicyStructure]],
) -> str:
    """Assemble the per-request user content the v3 system prompt expects.

    The candidate codes and policies come from date-resolved retrieval, so the
    model reasons over exactly the rules in force on the date of service and
    nothing from its training memory (CLAUDE.md section 4). Section labels
    here must match the ones the system prompt references (CANDIDATE CODES,
    POLICY CONTEXT, FACTS TO EXTRACT, CLAUSES TO ASSESS); change them together
    or the prompt contract silently breaks.

    Only note-sourced facts are requested from the model. History-sourced
    facts have no available source in this deployment; the rule engine
    resolves them to manual review in code, and asking the model for them
    would invite fabrication.
    """
    lines: list[str] = []

    lines.append("CLINICAL NOTE:")
    lines.append(note_text)
    lines.append("")

    lines.append("REQUEST:")
    lines.append(f"Requested service: {requested_service}")
    lines.append(f"Date of service: {date_of_service.isoformat()}")
    lines.append(f"Payer: {payer_name}")
    lines.append("")

    lines.append("CANDIDATE CODES (the only codes you may recommend):")
    for entry in candidate_codes:
        lines.append(f"- {entry.code} ({entry.code_system}): {entry.description}")
    lines.append("")

    for policy_row, structure in policies:
        lines.append(
            f"POLICY CONTEXT for {policy_row.policy_identifier} "
            f"({policy_row.payer_name}):"
        )
        lines.append(policy_row.policy_text)
        lines.append("")

        note_facts: list = []
        for fact_spec in structure.required_facts:
            if fact_spec.source == FACT_SOURCE_NOTE:
                note_facts.append(fact_spec)
        if len(note_facts) > 0:
            lines.append(
                f"FACTS TO EXTRACT for {policy_row.policy_identifier} "
                "(omit any the note does not state):"
            )
            for fact_spec in note_facts:
                unit_text = f" in {fact_spec.unit}" if fact_spec.unit else ""
                lines.append(
                    f"- {fact_spec.key} ({fact_spec.type}{unit_text}): "
                    f"{fact_spec.description}"
                )
            lines.append("")

        judgment_clauses: list = []
        for clause in structure.clauses:
            if clause.needs_judgment:
                judgment_clauses.append(clause)
        if len(judgment_clauses) > 0:
            lines.append(
                f"CLAUSES TO ASSESS for {policy_row.policy_identifier} "
                "(one judgment each, by clause_id):"
            )
            for clause in judgment_clauses:
                lines.append(
                    f"- clause_id {clause.clause_id}: {clause.judgment.question}"
                )
            lines.append("")

    return "\n".join(lines)

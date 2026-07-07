"""Prompt template loading and user-content assembly for the reasoning layer.

CLAUDE.md section 5 requires prompt templates in versioned files, not inline
strings, and requires logging which template version produced each output. A
template is a file named <name>_<version>.txt under src/medilens/prompts/; the
version travels with the loaded template and ends up in the audit record.

The template file is the static system prompt. Everything per-request (note,
metadata, retrieved codes and policies) is assembled into the user content by
build_user_content, so the system prompt stays byte-identical across requests,
which is also what makes it cacheable later.
"""

import datetime
from dataclasses import dataclass
from pathlib import Path

from medilens.db.models import CodeSetEntry, PayerPolicy

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_VALIDATION_PROMPT_NAME = "validation"
# v2: clause citations may legitimately be empty when no provided clause
# applies to a code (coverage decoupling); v1 required a clause per code.
# Old template files are never edited or deleted, so any audit record's
# prompt_template_version can be reproduced exactly.
DEFAULT_VALIDATION_PROMPT_VERSION = "v2"


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
    policies: list[PayerPolicy],
) -> str:
    """Assemble the per-request user content the system prompt expects.

    The candidate codes and policies come from date-resolved retrieval, so the
    model reasons over exactly the rules in force on the date of service and
    nothing from its training memory (CLAUDE.md section 4). Section labels here
    must match the ones the system prompt references (CANDIDATE CODES, PAYER
    POLICIES); change them together or the prompt contract silently breaks.
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

    lines.append("PAYER POLICIES (cite clauses by number):")
    for policy in policies:
        lines.append(
            f"Policy {policy.policy_identifier} ({policy.payer_name}):"
        )
        lines.append(policy.policy_text)
        lines.append("")

    return "\n".join(lines)

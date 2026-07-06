"""Ingest curated payer-policy seed files into the policy store.

The payer policy layer is the highest-value and hardest layer (CLAUDE.md
section 4): curated, versioned medical-necessity and prior-authorization
criteria for the beachhead. Every policy carries an effective date range and a
content hash so queries resolve against the date of service (guardrail 5) and
so a changed upstream policy is detected on re-ingestion (section 6).

Mirrors the code-set ingester in structure: parse_policy_seed_file turns a
YAML file into plain records with no database dependency, and ingest_policies
writes them. Parsing is kept separate from writing so the parser is
unit-testable without a database.

The seed's per-policy criteria are rendered into a single numbered policy_text
block so the reasoning layer can cite a specific clause by number
(guardrail 4). Rendering is deterministic, so the content hash is stable
across re-ingestion of unchanged policies.
"""

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from medilens.db.models import PayerPolicy
from medilens.hashing import hash_content


@dataclass(frozen=True)
class ParsedPolicy:
    """One payer-policy record parsed from a seed file, before the DB.

    Frozen because a parsed record is a value: the ingester hashes and writes
    it without altering it.
    """

    payer_name: str
    policy_identifier: str
    specialty: str
    policy_text: str
    effective_start: datetime.date
    effective_end: datetime.date | None
    source: str


def compute_policy_hash(policy: ParsedPolicy) -> str:
    """Hash the semantic fields that define a payer policy.

    Covers exactly the fields whose change should count as a new policy
    version: payer, identifier, specialty, the rendered criteria text, and the
    effective date range. Excludes retrieved_at, which is ingestion metadata.
    """
    effective_end_text = ""
    if policy.effective_end is not None:
        effective_end_text = policy.effective_end.isoformat()

    parts = [
        policy.payer_name,
        policy.policy_identifier,
        policy.specialty,
        policy.policy_text,
        policy.effective_start.isoformat(),
        effective_end_text,
    ]
    return hash_content(parts)


def _require_key(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Fetch a required key or fail loudly with a locating message.

    A malformed seed file is an error, not a partial load that silently drops
    policies (CLAUDE.md section 7).
    """
    if key not in mapping:
        raise ValueError(f"policy seed file {context} is missing required key: {key!r}")
    return mapping[key]


def _coerce_date(value: Any, context: str) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"policy seed file {context} has an invalid date: {value!r}"
            ) from exc
    raise ValueError(f"policy seed file {context} has a non-date value: {value!r}")


def _coerce_optional_date(value: Any, context: str) -> datetime.date | None:
    if value is None:
        return None
    return _coerce_date(value, context)


def render_policy_text(service: str, criteria: list[str]) -> str:
    """Render a service plus its criteria into a numbered, citable text block.

    The numbering lets a recommendation cite a specific clause (guardrail 4),
    and the deterministic layout keeps the content hash stable.
    """
    lines: list[str] = []
    lines.append(f"Service: {service}")
    lines.append("")
    lines.append("Documentation criteria:")
    clause_number = 1
    for criterion in criteria:
        lines.append(f"{clause_number}. {criterion}")
        clause_number += 1
    return "\n".join(lines)


def parse_policy_seed_file(seed_path: Path) -> list[ParsedPolicy]:
    """Parse a curated payer-policy YAML file into ParsedPolicy records.

    The file carries the specialty at the top level; each policy carries its
    payer, identifier, service, effective dates, source, and criteria list.
    """
    raw_text = seed_path.read_text(encoding="utf-8")
    document = yaml.safe_load(raw_text)
    if not isinstance(document, dict):
        raise ValueError(f"policy seed file {seed_path} did not parse to a mapping")

    context = str(seed_path)
    specialty = _require_key(document, "specialty", context)
    policy_rows = _require_key(document, "policies", context)
    if not isinstance(policy_rows, list):
        raise ValueError(f"policy seed file {context} key 'policies' must be a list")

    parsed_policies: list[ParsedPolicy] = []
    for policy_row in policy_rows:
        if not isinstance(policy_row, dict):
            raise ValueError(
                f"policy seed file {context} has a non-mapping policy entry"
            )
        payer_name = _require_key(policy_row, "payer_name", context)
        policy_identifier = _require_key(policy_row, "policy_identifier", context)
        service = _require_key(policy_row, "service", context)
        source = _require_key(policy_row, "source", context)
        effective_start = _coerce_date(
            _require_key(policy_row, "effective_start", context), context
        )
        effective_end = _coerce_optional_date(
            policy_row.get("effective_end"), context
        )
        criteria = _require_key(policy_row, "criteria", context)
        if not isinstance(criteria, list) or len(criteria) == 0:
            raise ValueError(
                f"policy seed file {context} policy {policy_identifier!r} "
                "must have a non-empty 'criteria' list"
            )

        policy_text = render_policy_text(service, criteria)
        policy = ParsedPolicy(
            payer_name=payer_name,
            policy_identifier=policy_identifier,
            specialty=specialty,
            policy_text=policy_text,
            effective_start=effective_start,
            effective_end=effective_end,
            source=source,
        )
        parsed_policies.append(policy)

    return parsed_policies


def ingest_policies(
    session: Session,
    parsed_policies: list[ParsedPolicy],
    retrieved_at: datetime.datetime,
) -> int:
    """Write parsed policies into the store, skipping unchanged ones.

    retrieved_at is supplied by the caller (not read from the clock here) so
    ingestion is deterministic and testable. A policy whose content hash
    already exists is skipped, so re-running ingestion is idempotent and only
    genuinely changed policies are re-written, preserving prior versions for
    audit.

    Returns the number of new rows written.
    """
    written_count = 0
    for policy in parsed_policies:
        content_hash = compute_policy_hash(policy)

        existing_query = select(PayerPolicy).where(
            PayerPolicy.content_hash == content_hash
        )
        existing_row = session.execute(existing_query).scalar_one_or_none()
        if existing_row is not None:
            continue

        row = PayerPolicy(
            payer_name=policy.payer_name,
            policy_identifier=policy.policy_identifier,
            specialty=policy.specialty,
            policy_text=policy.policy_text,
            effective_start=policy.effective_start,
            effective_end=policy.effective_end,
            source=policy.source,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
        )
        session.add(row)
        written_count += 1

    session.commit()
    return written_count

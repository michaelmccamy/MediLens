"""Ingest curated payer-policy seed files into the policy store.

The payer policy layer is the highest-value and hardest layer (CLAUDE.md
section 4): curated, versioned medical-necessity and prior-authorization
criteria for the beachhead. Every policy carries an effective date range and a
content hash so queries resolve against the date of service (guardrail 5) and
so a changed upstream policy is detected on re-ingestion (section 6).

Policies are policy schema v2 (docs/policy-schema.md): structured clauses with
stable clause_ids, evaluation types, deterministic rules, and judgment
questions. The structure is validated at parse time (a malformed policy fails
the ingest run loudly), stored canonically in structure_json, and also
rendered into a deterministic human-readable policy_text for coder display and
model context. The content hash covers the structure, so any clause change is
a new policy version and prior versions are preserved for audit.
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
from medilens.policy.lint import lint_policies
from medilens.policy.structure import (
    PolicyStructure,
    parse_policy_structure,
    render_structure_text,
    structure_to_json,
)


@dataclass(frozen=True)
class ParsedPolicy:
    """One payer-policy record parsed from a seed file, before the DB.

    Frozen because a parsed record is a value: the ingester hashes and writes
    it without altering it. structure is the validated policy-v2 content;
    structure_json is its canonical serialization (what gets stored and
    hashed); policy_text is the deterministic human-readable rendering.
    """

    payer_name: str
    policy_identifier: str
    specialty: str
    service: str
    service_keywords: str
    policy_text: str
    structure: PolicyStructure
    structure_json: str
    effective_start: datetime.date
    effective_end: datetime.date | None
    source: str


def compute_policy_hash(policy: ParsedPolicy) -> str:
    """Hash the semantic fields that define a payer policy.

    Covers exactly the fields whose change should count as a new policy
    version, including the canonical structure JSON, so any clause, rule, or
    fact change re-ingests as a new version. Excludes retrieved_at, which is
    ingestion metadata.
    """
    effective_end_text = ""
    if policy.effective_end is not None:
        effective_end_text = policy.effective_end.isoformat()

    parts = [
        policy.payer_name,
        policy.policy_identifier,
        policy.specialty,
        policy.service,
        policy.service_keywords,
        policy.structure_json,
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


def parse_policy_seed_file(seed_path: Path) -> list[ParsedPolicy]:
    """Parse a curated payer-policy YAML file into ParsedPolicy records.

    The file carries the specialty at the top level; each policy carries its
    payer, identifier, service, effective dates, source, and a policy-v2
    structure block, which is validated here (clause ids unique, evaluation
    types known, rules referencing declared facts, and so on).
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
        raw_keywords = _require_key(policy_row, "service_keywords", context)
        if not isinstance(raw_keywords, list) or len(raw_keywords) == 0:
            raise ValueError(
                f"policy seed file {context} policy {policy_identifier!r} "
                "must have a non-empty 'service_keywords' list; retrieval "
                "cannot match a policy to a requested service without them"
            )
        keyword_phrases: list[str] = []
        for keyword in raw_keywords:
            keyword_phrases.append(str(keyword).strip().lower())
        service_keywords = ",".join(keyword_phrases)
        source = _require_key(policy_row, "source", context)
        effective_start = _coerce_date(
            _require_key(policy_row, "effective_start", context), context
        )
        effective_end = _coerce_optional_date(
            policy_row.get("effective_end"), context
        )

        raw_structure = _require_key(policy_row, "structure", context)
        structure = parse_policy_structure(
            raw_structure, f"{context}:{policy_identifier}"
        )
        structure_json = structure_to_json(structure)
        policy_text = render_structure_text(service, structure)

        policy = ParsedPolicy(
            payer_name=payer_name,
            policy_identifier=policy_identifier,
            specialty=specialty,
            service=service,
            service_keywords=service_keywords,
            policy_text=policy_text,
            structure=structure,
            structure_json=structure_json,
            effective_start=effective_start,
            effective_end=effective_end,
            source=source,
        )
        parsed_policies.append(policy)

    return parsed_policies


def _reject_ambiguous_policy_set(
    session: Session, parsed_policies: list[ParsedPolicy]
) -> None:
    """Fail loudly when the post-ingest current set would be ambiguous.

    The lint runs on the projection of what will be current after the run:
    the incoming batch, plus every current database row whose payer and
    identifier the batch does not carry (those rows survive the run
    unchanged, so a conflict with them is just as live as one inside the
    batch). Rows the batch does carry are about to be superseded or matched,
    so their old keywords are excluded: a batch that fixes an ambiguity must
    not be blocked by the very rows it replaces.
    """
    batch_identities: set[tuple[str, str]] = set()
    for policy in parsed_policies:
        batch_identities.add((policy.payer_name, policy.policy_identifier))

    surviving_query = select(PayerPolicy).where(PayerPolicy.superseded_at.is_(None))
    surviving_rows = session.execute(surviving_query).scalars().all()

    projected: list = list(parsed_policies)
    for row in surviving_rows:
        if (row.payer_name, row.policy_identifier) in batch_identities:
            continue
        projected.append(row)

    problems = lint_policies(projected)
    if len(problems) > 0:
        problem_lines = "\n- ".join(problems)
        raise ValueError(
            "refusing to ingest an ambiguous policy set; fix these before "
            f"loading:\n- {problem_lines}"
        )


@dataclass(frozen=True)
class PolicyIngestResult:
    """Counts from one policy ingest run.

    written is the number of new version rows inserted. superseded is the
    number of previously-current rows stamped as replaced, which makes version
    rolls visible to the operator instead of silent.
    """

    written: int
    superseded: int


def ingest_policies(
    session: Session,
    parsed_policies: list[ParsedPolicy],
    retrieved_at: datetime.datetime,
) -> PolicyIngestResult:
    """Write parsed policies into the store, superseding replaced versions.

    retrieved_at is supplied by the caller (not read from the clock here) so
    ingestion is deterministic and testable. For each parsed policy, the
    current rows (superseded_at NULL) for its payer and identifier decide the
    outcome:

    - A current row with the same content hash means the policy is unchanged:
      nothing is written, so re-running ingestion is idempotent. Any other
      current rows for that identifier are stale duplicates (from before
      supersession existed) and are stamped superseded.
    - No matching current row means the content changed: a new row is written
      and every previously-current row is stamped superseded.

    Superseded rows are never deleted or content-modified; the stamp is
    versioning metadata that keeps prior versions queryable for audit while
    guaranteeing retrieval sees exactly one current version per identifier.
    Retrieval consulting a stale version's service keywords was a live bug
    (see test_superseded_version_keywords_do_not_match); superseding at ingest
    removes the ambiguity at the source.

    Before anything is written, the set of policies that would be current
    after this run (the batch plus the database's current rows the batch does
    not replace) is linted for keyword ambiguity, and any problem aborts the
    whole run. Loading an ambiguous set would let one request be judged
    against two different services, so failing loudly here is the guard.
    """
    _reject_ambiguous_policy_set(session, parsed_policies)

    written_count = 0
    superseded_count = 0
    for policy in parsed_policies:
        content_hash = compute_policy_hash(policy)

        current_query = select(PayerPolicy).where(
            (PayerPolicy.payer_name == policy.payer_name)
            & (PayerPolicy.policy_identifier == policy.policy_identifier)
            & (PayerPolicy.superseded_at.is_(None))
        )
        current_rows = list(session.execute(current_query).scalars().all())

        # Keep the newest current row whose content matches; everything else
        # current for this identifier is being replaced.
        kept_row: PayerPolicy | None = None
        for row in current_rows:
            if row.content_hash != content_hash:
                continue
            if kept_row is None or row.retrieved_at > kept_row.retrieved_at:
                kept_row = row

        for row in current_rows:
            if row is kept_row:
                continue
            row.superseded_at = retrieved_at
            superseded_count += 1

        if kept_row is not None:
            continue

        new_row = PayerPolicy(
            payer_name=policy.payer_name,
            policy_identifier=policy.policy_identifier,
            specialty=policy.specialty,
            service=policy.service,
            service_keywords=policy.service_keywords,
            policy_text=policy.policy_text,
            structure_json=policy.structure_json,
            effective_start=policy.effective_start,
            effective_end=policy.effective_end,
            source=policy.source,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
        )
        session.add(new_row)
        written_count += 1

    session.commit()
    return PolicyIngestResult(written=written_count, superseded=superseded_count)

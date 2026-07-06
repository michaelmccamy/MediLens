"""Ingest curated code-set seed files into the knowledge store.

The knowledge layer is retrieval-augmented: the model reasons over the codes
this ingester loads, not codes recalled from training memory (CLAUDE.md
section 4). Every entry carries an effective date range and a content hash so
that queries can resolve against the date of service (guardrail 5) and so that
a changed upstream description or date is detected on re-ingestion
(section 6).

Parsing is kept separate from database writing: parse_seed_file turns a YAML
file into plain records with no database dependency, and ingest_records writes
them. This makes the parser unit-testable without a database and lets the same
records feed a future full-CMS-file path unchanged.
"""

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from medilens.db.models import CodeSetEntry


@dataclass(frozen=True)
class ParsedCodeEntry:
    """One code-set record parsed from a seed file, before it reaches the DB.

    Frozen because a parsed record is a value, not mutable state: the ingester
    hashes it and writes it without altering it.
    """

    code_system: str
    code: str
    description: str
    effective_start: datetime.date
    effective_end: datetime.date | None
    source: str


def compute_content_hash(entry: ParsedCodeEntry) -> str:
    """Hash the semantic fields that define a code-set entry.

    CLAUDE.md section 6 requires a hash so upstream changes can be detected and
    re-ingested. The hash covers exactly the fields whose change should count
    as a new version: code system, code, description, and the effective date
    range. It deliberately excludes retrieved_at, which is ingestion metadata
    and would otherwise make every re-ingestion look like a change.
    """
    effective_end_text = ""
    if entry.effective_end is not None:
        effective_end_text = entry.effective_end.isoformat()

    # A field separator that cannot appear in the values keeps the hash
    # unambiguous, so two different field splits cannot produce the same string.
    parts = [
        entry.code_system,
        entry.code,
        entry.description,
        entry.effective_start.isoformat(),
        effective_end_text,
    ]
    canonical = "\x1f".join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest


def _require_key(mapping: dict[str, Any], key: str, context: str) -> Any:
    """Fetch a required key or fail loudly with a locating message.

    CLAUDE.md section 7 requires failing loudly on missing data rather than
    silently guessing, so a malformed seed file is an error, not a partial
    load that silently drops codes.
    """
    if key not in mapping:
        raise ValueError(f"seed file {context} is missing required key: {key!r}")
    return mapping[key]


def _coerce_date(value: Any, context: str) -> datetime.date:
    """Accept a date already parsed by YAML, or an ISO date string."""
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"seed file {context} has an invalid date: {value!r}"
            ) from exc
    raise ValueError(f"seed file {context} has a non-date value: {value!r}")


def _coerce_optional_date(value: Any, context: str) -> datetime.date | None:
    if value is None:
        return None
    return _coerce_date(value, context)


def parse_seed_file(seed_path: Path) -> list[ParsedCodeEntry]:
    """Parse a curated seed YAML file into ParsedCodeEntry records.

    The file carries a code_system, source, and default effective date range at
    the top level. Each code inherits those defaults and may override the
    effective dates individually.
    """
    raw_text = seed_path.read_text(encoding="utf-8")
    document = yaml.safe_load(raw_text)
    if not isinstance(document, dict):
        raise ValueError(f"seed file {seed_path} did not parse to a mapping")

    context = str(seed_path)
    code_system = _require_key(document, "code_system", context)
    source = _require_key(document, "source", context)
    default_effective_start = _coerce_date(
        _require_key(document, "effective_start", context), context
    )
    default_effective_end = _coerce_optional_date(
        document.get("effective_end"), context
    )
    code_rows = _require_key(document, "codes", context)
    if not isinstance(code_rows, list):
        raise ValueError(f"seed file {context} key 'codes' must be a list")

    parsed_entries: list[ParsedCodeEntry] = []
    for code_row in code_rows:
        if not isinstance(code_row, dict):
            raise ValueError(f"seed file {context} has a non-mapping code entry")
        code = _require_key(code_row, "code", context)
        description = _require_key(code_row, "description", context)

        # Per-code effective dates are optional; fall back to the file defaults.
        if "effective_start" in code_row:
            effective_start = _coerce_date(code_row["effective_start"], context)
        else:
            effective_start = default_effective_start
        if "effective_end" in code_row:
            effective_end = _coerce_optional_date(code_row["effective_end"], context)
        else:
            effective_end = default_effective_end

        entry = ParsedCodeEntry(
            code_system=code_system,
            code=code,
            description=description,
            effective_start=effective_start,
            effective_end=effective_end,
            source=source,
        )
        parsed_entries.append(entry)

    return parsed_entries


def ingest_records(
    session: Session,
    parsed_entries: list[ParsedCodeEntry],
    retrieved_at: datetime.datetime,
) -> int:
    """Write parsed records into the knowledge store, skipping unchanged ones.

    retrieved_at is supplied by the caller (not read from the clock here) so
    ingestion is deterministic and testable. A record whose content hash
    already exists is skipped, so re-running ingestion is idempotent and only
    genuinely changed codes are re-written.

    Returns the number of new rows written.
    """
    written_count = 0
    for entry in parsed_entries:
        content_hash = compute_content_hash(entry)

        existing_query = select(CodeSetEntry).where(
            CodeSetEntry.content_hash == content_hash
        )
        existing_row = session.execute(existing_query).scalar_one_or_none()
        if existing_row is not None:
            continue

        row = CodeSetEntry(
            code_system=entry.code_system,
            code=entry.code,
            description=entry.description,
            effective_start=entry.effective_start,
            effective_end=entry.effective_end,
            source=entry.source,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
        )
        session.add(row)
        written_count += 1

    session.commit()
    return written_count

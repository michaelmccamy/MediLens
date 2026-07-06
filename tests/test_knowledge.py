"""Tests for the knowledge layer: parsing, hashing, ingestion, date resolution.

Uses an in-memory SQLite database so the suite runs in CI without Docker. The
ORM models are database-agnostic, so this exercises the same query logic that
runs against Postgres in dev. No real PHI is involved (CLAUDE.md section 8).
"""

import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import medilens.knowledge as knowledge_package
from medilens.db.models import Base, CodeSetEntry
from medilens.knowledge.ingest import (
    ParsedCodeEntry,
    compute_content_hash,
    ingest_records,
    parse_seed_file,
)
from medilens.knowledge.retrieval import find_code_at_date, list_codes_at_date

SEED_PATH = (
    Path(knowledge_package.__file__).parent / "seed" / "icd10cm_ortho_pain.yaml"
)

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)


@pytest.fixture
def session() -> Session:
    """An in-memory SQLite session with the schema created."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _make_entry(
    code: str = "M54.16",
    description: str = "Radiculopathy, lumbar region",
    effective_start: datetime.date = datetime.date(2025, 10, 1),
    effective_end: datetime.date | None = None,
) -> ParsedCodeEntry:
    return ParsedCodeEntry(
        code_system="ICD-10-CM",
        code=code,
        description=description,
        effective_start=effective_start,
        effective_end=effective_end,
        source="test source",
    )


# --- parsing -------------------------------------------------------------


def test_parse_seed_file_loads_curated_codes() -> None:
    entries = parse_seed_file(SEED_PATH)

    assert len(entries) > 0
    codes = {entry.code for entry in entries}
    # Spot-check beachhead-relevant codes are present.
    assert "M54.16" in codes  # lumbar radiculopathy
    assert "M51.36" in codes  # lumbar disc degeneration


def test_parse_seed_file_applies_default_effective_dates() -> None:
    entries = parse_seed_file(SEED_PATH)

    for entry in entries:
        assert entry.code_system == "ICD-10-CM"
        assert entry.effective_start == datetime.date(2025, 10, 1)
        assert entry.effective_end is None


def test_parse_seed_file_rejects_missing_key(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("code_system: ICD-10-CM\n", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_seed_file(bad_file)


# --- hashing -------------------------------------------------------------


def test_content_hash_is_stable() -> None:
    entry = _make_entry()

    assert compute_content_hash(entry) == compute_content_hash(entry)


def test_content_hash_changes_when_description_changes() -> None:
    original = _make_entry(description="Radiculopathy, lumbar region")
    revised = _make_entry(description="Radiculopathy, lumbar region (revised)")

    assert compute_content_hash(original) != compute_content_hash(revised)


def test_content_hash_changes_when_effective_end_set() -> None:
    active = _make_entry(effective_end=None)
    retired = _make_entry(effective_end=datetime.date(2026, 9, 30))

    assert compute_content_hash(active) != compute_content_hash(retired)


# --- ingestion -----------------------------------------------------------


def test_ingest_writes_rows(session: Session) -> None:
    entries = parse_seed_file(SEED_PATH)

    written = ingest_records(session, entries, FIXED_RETRIEVED_AT)

    assert written == len(entries)
    stored = session.query(CodeSetEntry).count()
    assert stored == len(entries)


def test_ingest_is_idempotent(session: Session) -> None:
    entries = parse_seed_file(SEED_PATH)

    first = ingest_records(session, entries, FIXED_RETRIEVED_AT)
    second = ingest_records(session, entries, FIXED_RETRIEVED_AT)

    assert first == len(entries)
    # Nothing changed, so the second run writes no new rows.
    assert second == 0
    assert session.query(CodeSetEntry).count() == len(entries)


def test_ingest_writes_new_row_when_content_changes(session: Session) -> None:
    original = _make_entry(description="Radiculopathy, lumbar region")
    ingest_records(session, [original], FIXED_RETRIEVED_AT)

    revised = _make_entry(description="Radiculopathy, lumbar region (2026 wording)")
    written = ingest_records(session, [revised], FIXED_RETRIEVED_AT)

    # The revised description hashes differently, so it is a new version and
    # both rows coexist for audit history.
    assert written == 1
    assert session.query(CodeSetEntry).count() == 2


# --- date-resolved retrieval ---------------------------------------------


def test_find_code_in_force(session: Session) -> None:
    ingest_records(session, [_make_entry()], FIXED_RETRIEVED_AT)

    found = find_code_at_date(
        session, "ICD-10-CM", "M54.16", datetime.date(2026, 6, 1)
    )

    assert found is not None
    assert found.code == "M54.16"


def test_find_code_before_effective_start_returns_none(session: Session) -> None:
    ingest_records(
        session,
        [_make_entry(effective_start=datetime.date(2025, 10, 1))],
        FIXED_RETRIEVED_AT,
    )

    # Date of service predates the code's effective start.
    found = find_code_at_date(
        session, "ICD-10-CM", "M54.16", datetime.date(2025, 9, 30)
    )

    assert found is None


def test_effective_start_boundary_is_inclusive(session: Session) -> None:
    ingest_records(
        session,
        [_make_entry(effective_start=datetime.date(2025, 10, 1))],
        FIXED_RETRIEVED_AT,
    )

    found = find_code_at_date(
        session, "ICD-10-CM", "M54.16", datetime.date(2025, 10, 1)
    )

    assert found is not None


def test_effective_end_boundary_is_inclusive(session: Session) -> None:
    ingest_records(
        session,
        [_make_entry(effective_end=datetime.date(2026, 9, 30))],
        FIXED_RETRIEVED_AT,
    )

    on_last_valid_day = find_code_at_date(
        session, "ICD-10-CM", "M54.16", datetime.date(2026, 9, 30)
    )
    day_after = find_code_at_date(
        session, "ICD-10-CM", "M54.16", datetime.date(2026, 10, 1)
    )

    assert on_last_valid_day is not None
    assert day_after is None


def test_list_codes_at_date_excludes_out_of_force(session: Session) -> None:
    active = _make_entry(code="M54.16", effective_end=None)
    retired = _make_entry(
        code="M54.99",
        description="Old code",
        effective_end=datetime.date(2025, 9, 30),
    )
    ingest_records(session, [active, retired], FIXED_RETRIEVED_AT)

    in_force = list_codes_at_date(
        session, "ICD-10-CM", datetime.date(2026, 6, 1)
    )

    returned_codes = {entry.code for entry in in_force}
    assert "M54.16" in returned_codes
    assert "M54.99" not in returned_codes


def test_list_codes_at_date_is_ordered(session: Session) -> None:
    entries = [
        _make_entry(code="M54.59", description="Other low back pain"),
        _make_entry(code="M51.36", description="Lumbar disc degeneration"),
        _make_entry(code="M48.06", description="Lumbar spinal stenosis"),
    ]
    ingest_records(session, entries, FIXED_RETRIEVED_AT)

    in_force = list_codes_at_date(
        session, "ICD-10-CM", datetime.date(2026, 6, 1)
    )

    returned_codes = [entry.code for entry in in_force]
    assert returned_codes == sorted(returned_codes)

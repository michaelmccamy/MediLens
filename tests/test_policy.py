"""Tests for the payer-policy layer: parsing, hashing, ingestion, date resolution.

Uses in-memory SQLite so the suite runs in CI without Docker. No real PHI is
involved, and the seed policies are synthetic (CLAUDE.md section 8).
"""

import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import medilens.policy as policy_package
from medilens.db.models import Base, PayerPolicy
from medilens.policy.ingest import (
    ParsedPolicy,
    compute_policy_hash,
    ingest_policies,
    parse_policy_seed_file,
    render_policy_text,
)
from medilens.policy.retrieval import (
    find_policy_at_date,
    list_policies_for_payer_at_date,
)

SEED_PATH = (
    Path(policy_package.__file__).parent / "seed" / "payer_policies_ortho_pain.yaml"
)

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _make_policy(
    payer_name: str = "Medicare",
    policy_identifier: str = "SYN-LUMBAR-MRI-001",
    policy_text: str = "Service: Lumbar MRI\n\nDocumentation criteria:\n1. Example.",
    effective_start: datetime.date = datetime.date(2025, 1, 1),
    effective_end: datetime.date | None = None,
) -> ParsedPolicy:
    return ParsedPolicy(
        payer_name=payer_name,
        policy_identifier=policy_identifier,
        specialty="Orthopedics and pain medicine",
        policy_text=policy_text,
        effective_start=effective_start,
        effective_end=effective_end,
        source="test source",
    )


# --- rendering and parsing ----------------------------------------------


def test_render_policy_text_numbers_criteria() -> None:
    rendered = render_policy_text(
        "Lumbar MRI", ["First requirement.", "Second requirement."]
    )

    assert "Service: Lumbar MRI" in rendered
    assert "1. First requirement." in rendered
    assert "2. Second requirement." in rendered


def test_parse_seed_file_loads_policies() -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    assert len(policies) > 0
    identifiers = {policy.policy_identifier for policy in policies}
    assert "SYN-LUMBAR-MRI-001" in identifiers


def test_parse_seed_file_applies_specialty_and_renders_text() -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    for policy in policies:
        assert policy.specialty == "Orthopedics and pain medicine"
        # Criteria are rendered into a numbered, citable block.
        assert "Documentation criteria:" in policy.policy_text
        assert "1. " in policy.policy_text


def test_parse_seed_file_rejects_empty_criteria(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_policy.yaml"
    bad_file.write_text(
        "specialty: X\n"
        "policies:\n"
        "  - payer_name: P\n"
        "    policy_identifier: ID\n"
        "    service: S\n"
        "    source: src\n"
        "    effective_start: 2025-01-01\n"
        "    criteria: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        parse_policy_seed_file(bad_file)


# --- hashing -------------------------------------------------------------


def test_policy_hash_is_stable() -> None:
    policy = _make_policy()

    assert compute_policy_hash(policy) == compute_policy_hash(policy)


def test_policy_hash_changes_when_text_changes() -> None:
    original = _make_policy(policy_text="1. Original clause.")
    revised = _make_policy(policy_text="1. Revised clause.")

    assert compute_policy_hash(original) != compute_policy_hash(revised)


def test_policy_hash_changes_when_effective_end_set() -> None:
    active = _make_policy(effective_end=None)
    retired = _make_policy(effective_end=datetime.date(2025, 12, 31))

    assert compute_policy_hash(active) != compute_policy_hash(retired)


# --- ingestion -----------------------------------------------------------


def test_ingest_writes_rows(session: Session) -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    written = ingest_policies(session, policies, FIXED_RETRIEVED_AT)

    assert written == len(policies)
    assert session.query(PayerPolicy).count() == len(policies)


def test_ingest_is_idempotent(session: Session) -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    first = ingest_policies(session, policies, FIXED_RETRIEVED_AT)
    second = ingest_policies(session, policies, FIXED_RETRIEVED_AT)

    assert first == len(policies)
    assert second == 0
    assert session.query(PayerPolicy).count() == len(policies)


def test_ingest_writes_new_version_when_text_changes(session: Session) -> None:
    original = _make_policy(policy_text="1. Original clause.")
    ingest_policies(session, [original], FIXED_RETRIEVED_AT)

    revised = _make_policy(policy_text="1. Revised clause for 2026.")
    written = ingest_policies(session, [revised], FIXED_RETRIEVED_AT)

    # A changed policy is a new version; both coexist for audit history.
    assert written == 1
    assert session.query(PayerPolicy).count() == 2


# --- date-resolved retrieval ---------------------------------------------


def test_find_policy_in_force(session: Session) -> None:
    ingest_policies(session, [_make_policy()], FIXED_RETRIEVED_AT)

    found = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2026, 6, 1)
    )

    assert found is not None
    assert found.policy_identifier == "SYN-LUMBAR-MRI-001"


def test_find_policy_before_effective_start_returns_none(session: Session) -> None:
    ingest_policies(
        session,
        [_make_policy(effective_start=datetime.date(2025, 1, 1))],
        FIXED_RETRIEVED_AT,
    )

    found = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2024, 12, 31)
    )

    assert found is None


def test_find_policy_effective_end_boundary_is_inclusive(session: Session) -> None:
    ingest_policies(
        session,
        [_make_policy(effective_end=datetime.date(2025, 12, 31))],
        FIXED_RETRIEVED_AT,
    )

    on_last_day = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2025, 12, 31)
    )
    day_after = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2026, 1, 1)
    )

    assert on_last_day is not None
    assert day_after is None


def test_list_policies_filters_by_payer_and_specialty(session: Session) -> None:
    medicare = _make_policy(payer_name="Medicare", policy_identifier="SYN-A")
    commercial = _make_policy(
        payer_name="National Commercial Payer A", policy_identifier="SYN-B"
    )
    ingest_policies(session, [medicare, commercial], FIXED_RETRIEVED_AT)

    medicare_policies = list_policies_for_payer_at_date(
        session,
        "Medicare",
        "Orthopedics and pain medicine",
        datetime.date(2026, 6, 1),
    )

    returned_ids = {policy.policy_identifier for policy in medicare_policies}
    assert "SYN-A" in returned_ids
    assert "SYN-B" not in returned_ids


def test_list_policies_excludes_out_of_force(session: Session) -> None:
    active = _make_policy(policy_identifier="SYN-ACTIVE", effective_end=None)
    retired = _make_policy(
        policy_identifier="SYN-RETIRED",
        policy_text="1. Old policy.",
        effective_end=datetime.date(2024, 12, 31),
    )
    ingest_policies(session, [active, retired], FIXED_RETRIEVED_AT)

    in_force = list_policies_for_payer_at_date(
        session,
        "Medicare",
        "Orthopedics and pain medicine",
        datetime.date(2026, 6, 1),
    )

    returned_ids = {policy.policy_identifier for policy in in_force}
    assert "SYN-ACTIVE" in returned_ids
    assert "SYN-RETIRED" not in returned_ids

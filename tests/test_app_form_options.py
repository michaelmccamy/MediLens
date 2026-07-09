"""Tests for the review form options derived from loaded policies.

The service and payer dropdowns must reflect the policies actually loaded
(current versions only), because a hardcoded list drifts every time a policy
is ingested: the hip injection policy was invisible in the form for exactly
that reason. Uses a file-backed SQLite database so the app's own
engine-from-settings path is exercised.
"""

import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.db.models import Base, PayerPolicy
from medilens.ingestion import run_ingestion
from medilens.ui.app import (
    FALLBACK_PAYERS,
    FALLBACK_SERVICE_OPTIONS,
    REFUSAL_DEMO_SERVICE,
    _load_form_options,
)

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)


def _seeded_settings(tmp_path: Path) -> SimpleNamespace:
    """Build a file-backed database with the real seeds and settings for it."""
    database_url = f"sqlite:///{tmp_path / 'form_options.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run_ingestion(session, FIXED_RETRIEVED_AT)
    return SimpleNamespace(database_url=database_url)


def test_form_options_derive_from_loaded_policies(tmp_path: Path) -> None:
    settings = _seeded_settings(tmp_path)

    services, payers = _load_form_options(settings)

    assert "Major joint injection, hip (intra-articular)" in services
    assert "Major joint injection, knee (intra-articular)" in services
    assert "Lumbar MRI (advanced imaging of the lumbar spine)" in services
    assert "Medicare" in payers
    assert "National Commercial Payer A" in payers
    assert "National Commercial Payer B" in payers
    # The refusal-demo service is always offered, and last.
    assert services[-1] == REFUSAL_DEMO_SERVICE


def test_form_options_exclude_superseded_policies(tmp_path: Path) -> None:
    settings = _seeded_settings(tmp_path)
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        # Supersede every version of the hip policy directly; its service must
        # drop out of the form because retrieval would refuse it anyway.
        hip_rows = (
            session.query(PayerPolicy)
            .filter(PayerPolicy.policy_identifier == "SYN-HIP-INJ-001")
            .all()
        )
        for row in hip_rows:
            row.superseded_at = FIXED_RETRIEVED_AT
        session.commit()

    services, _payers = _load_form_options(settings)

    assert "Major joint injection, hip (intra-articular)" not in services
    assert "Major joint injection, knee (intra-articular)" in services


def test_form_options_fall_back_in_sample_mode() -> None:
    services, payers = _load_form_options(None)

    assert services == FALLBACK_SERVICE_OPTIONS + [REFUSAL_DEMO_SERVICE]
    assert payers == FALLBACK_PAYERS


def test_form_options_fall_back_when_database_unreachable(
    tmp_path: Path,
) -> None:
    # A database path inside a directory that does not exist fails on connect;
    # the form still renders with the static fallback rather than crashing.
    missing = tmp_path / "no_such_dir" / "db.sqlite"
    settings = SimpleNamespace(database_url=f"sqlite:///{missing}")

    services, payers = _load_form_options(settings)

    assert services == FALLBACK_SERVICE_OPTIONS + [REFUSAL_DEMO_SERVICE]
    assert payers == FALLBACK_PAYERS

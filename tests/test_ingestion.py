"""Tests for the ingestion orchestrator that loads both seed files."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medilens.db.models import Base, CodeSetEntry, PayerPolicy
from medilens.ingestion import run_ingestion

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def test_run_ingestion_loads_both_stores(session: Session) -> None:
    summary = run_ingestion(session, FIXED_RETRIEVED_AT)

    assert summary.code_entries_written > 0
    assert summary.policies_written > 0
    assert session.query(CodeSetEntry).count() == summary.code_entries_written
    assert session.query(PayerPolicy).count() == summary.policies_written


def test_run_ingestion_is_idempotent(session: Session) -> None:
    first = run_ingestion(session, FIXED_RETRIEVED_AT)
    second = run_ingestion(session, FIXED_RETRIEVED_AT)

    assert first.code_entries_written > 0
    assert first.policies_written > 0
    # A second run over unchanged seeds writes and supersedes nothing.
    assert second.code_entries_written == 0
    assert second.policies_written == 0
    assert second.policies_superseded == 0
    # Row counts are unchanged from the first run.
    assert session.query(CodeSetEntry).count() == first.code_entries_written
    assert session.query(PayerPolicy).count() == first.policies_written

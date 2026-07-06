"""Orchestrates loading the curated seed files into the knowledge stores.

Ties together the knowledge (code-set) and payer-policy ingesters so a single
command loads both. Locating the seed files and sequencing the two ingesters
lives here rather than in the CLI, so the CLI stays a thin entrypoint and this
orchestration is unit-testable against an in-memory database.
"""

import datetime
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

import medilens.knowledge as knowledge_package
import medilens.policy as policy_package
from medilens.knowledge.ingest import ingest_records, parse_seed_file
from medilens.policy.ingest import ingest_policies, parse_policy_seed_file


@dataclass
class IngestionSummary:
    """Counts of new rows written by one ingestion run.

    Zero counts on a re-run are expected and correct: ingestion is idempotent,
    so unchanged seeds write nothing the second time.
    """

    code_entries_written: int
    policies_written: int


def default_code_seed_path() -> Path:
    return (
        Path(knowledge_package.__file__).parent
        / "seed"
        / "icd10cm_ortho_pain.yaml"
    )


def default_policy_seed_path() -> Path:
    return (
        Path(policy_package.__file__).parent
        / "seed"
        / "payer_policies_ortho_pain.yaml"
    )


def run_ingestion(
    session: Session,
    retrieved_at: datetime.datetime,
    code_seed_path: Path | None = None,
    policy_seed_path: Path | None = None,
) -> IngestionSummary:
    """Parse and load both seed files, returning the counts written.

    retrieved_at is supplied by the caller so ingestion is deterministic and
    testable. Seed paths default to the bundled curated seeds but can be
    overridden, which is how the full-CMS-file path will plug in later.
    """
    if code_seed_path is None:
        code_seed_path = default_code_seed_path()
    if policy_seed_path is None:
        policy_seed_path = default_policy_seed_path()

    parsed_codes = parse_seed_file(code_seed_path)
    code_entries_written = ingest_records(session, parsed_codes, retrieved_at)

    parsed_policies = parse_policy_seed_file(policy_seed_path)
    policies_written = ingest_policies(session, parsed_policies, retrieved_at)

    summary = IngestionSummary(
        code_entries_written=code_entries_written,
        policies_written=policies_written,
    )
    return summary

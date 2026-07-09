"""Tests for CLI argument parsing, subcommand wiring, and the check-policies
dry run.

The parser tests need no external setup. The check-policies tests run the
real handler against the bundled seed and a deliberately ambiguous file, with
no database or model calls (that is the point of the command).
"""

import argparse
from pathlib import Path

import pytest

import medilens.policy as policy_package
from medilens.cli import (
    build_arg_parser,
    run_check_policies_command,
    run_ingest_command,
    run_validate_command,
)

SEED_PATH = (
    Path(policy_package.__file__).parent / "seed" / "payer_policies_ortho_pain.yaml"
)

# A structurally valid file whose two policies share a joint-less keyword:
# the exact ambiguity class the lint exists to reject.
AMBIGUOUS_POLICY_YAML = """\
specialty: Orthopedics and pain medicine
policies:
  - payer_name: Medicare
    policy_identifier: SYN-KNEE-TEST
    effective_start: 2025-01-01
    effective_end: null
    service: "Major joint injection, knee"
    service_keywords: ["knee injection", "major joint injection"]
    source: "SYNTHETIC test policy."
    structure: &structure
      schema_version: policy-v2
      version: 1
      source: {type: synthetic, authoritative: false, citation: "test"}
      required_facts:
        - key: symptom_duration
          type: duration
          unit: weeks
          source: note
          description: "duration"
      clauses:
        - clause_id: duration
          title: "Duration"
          text: "Documented duration."
          evaluation: hybrid
          required: true
          rule: {op: min_duration, fact: symptom_duration, unit: weeks, minimum: 6}
          judgment: {question: "Documented?", requires_evidence: true}
  - payer_name: Medicare
    policy_identifier: SYN-HIP-TEST
    effective_start: 2025-01-01
    effective_end: null
    service: "Major joint injection, hip"
    service_keywords: ["hip injection", "major joint injection"]
    source: "SYNTHETIC test policy."
    structure: *structure
"""


def test_ingest_subcommand_dispatches_to_ingest_handler() -> None:
    parser = build_arg_parser()

    args = parser.parse_args(["ingest"])

    assert args.handler is run_ingest_command
    assert args.policy_seed is None


def test_ingest_accepts_policy_seed_override() -> None:
    parser = build_arg_parser()

    args = parser.parse_args(["ingest", "--policy-seed", "my_policies.yaml"])

    assert args.policy_seed == "my_policies.yaml"


def test_check_policies_subcommand_dispatches() -> None:
    parser = build_arg_parser()

    args = parser.parse_args(["check-policies", "my_policies.yaml"])

    assert args.handler is run_check_policies_command
    assert args.policy_path == "my_policies.yaml"


def test_check_policies_reports_the_bundled_seed_clean(capsys) -> None:
    args = argparse.Namespace(policy_path=str(SEED_PATH))

    run_check_policies_command(None, args)

    output = capsys.readouterr().out
    assert "clean" in output
    assert "SYN-HIP-INJ-001" in output


def test_check_policies_exits_nonzero_on_ambiguous_file(
    tmp_path: Path, capsys
) -> None:
    policy_file = tmp_path / "ambiguous.yaml"
    policy_file.write_text(AMBIGUOUS_POLICY_YAML, encoding="utf-8")
    args = argparse.Namespace(policy_path=str(policy_file))

    with pytest.raises(SystemExit):
        run_check_policies_command(None, args)

    output = capsys.readouterr().out
    assert "PROBLEMS" in output
    assert "ambiguous" in output


def test_check_policies_missing_file_fails_loudly() -> None:
    args = argparse.Namespace(policy_path="no_such_file.yaml")

    with pytest.raises(SystemExit, match="not found"):
        run_check_policies_command(None, args)


def test_validate_subcommand_dispatches_to_validate_handler() -> None:
    parser = build_arg_parser()

    args = parser.parse_args(
        [
            "validate",
            "note.txt",
            "--requested-service",
            "lumbar MRI",
            "--date-of-service",
            "2026-06-01",
            "--payer",
            "Medicare",
        ]
    )

    assert args.handler is run_validate_command
    assert args.note_path == "note.txt"
    assert args.requested_service == "lumbar MRI"
    assert args.payer == "Medicare"


def test_no_subcommand_is_an_error() -> None:
    parser = build_arg_parser()

    # A missing subcommand should exit rather than silently doing nothing.
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_validate_requires_service_and_payer() -> None:
    parser = build_arg_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["validate", "note.txt"])

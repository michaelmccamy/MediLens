"""Tests for CLI argument parsing and subcommand wiring.

These exercise the parser only, with no database or model calls, so they run
without any external setup.
"""

import pytest

from medilens.cli import build_arg_parser, run_ingest_command, run_validate_command


def test_ingest_subcommand_dispatches_to_ingest_handler() -> None:
    parser = build_arg_parser()

    args = parser.parse_args(["ingest"])

    assert args.handler is run_ingest_command


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

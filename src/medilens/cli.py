"""Command-line entrypoint.

Two subcommands:

- ingest: load the curated code-set and payer-policy seeds into the database.
- validate: take a synthetic clinical note plus request metadata and (once the
  reasoning layer exists) print a coding recommendation with citations and a
  denial-risk score. Today it is a stub that validates configuration and
  arguments so the command shape is settled before that logic is built.

The CLI stays thin: it parses arguments, loads settings, and delegates to the
orchestration and ingestion modules.
"""

import argparse
import datetime
import sys

from medilens.config import Settings, load_settings
from medilens.db.session import build_engine, build_session_factory, create_all_tables
from medilens.ingestion import run_ingestion


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medilens",
        description="Pre-claim denial prevention and documentation sufficiency check.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Load the curated code-set and payer-policy seeds into the database.",
    )
    ingest_parser.set_defaults(handler=run_ingest_command)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Check a synthetic note for documentation sufficiency (stub).",
    )
    validate_parser.add_argument(
        "note_path",
        help="Path to a synthetic, de-identified clinical note text file.",
    )
    validate_parser.add_argument(
        "--requested-service",
        required=True,
        help="Plain-language service requested, for example 'lumbar MRI'.",
    )
    validate_parser.add_argument(
        "--date-of-service",
        required=True,
        help="Date of service in YYYY-MM-DD format. Code sets and payer "
        "policy are resolved against this date, not today.",
    )
    validate_parser.add_argument(
        "--payer",
        required=True,
        help="Payer name, for example 'Medicare' or a commercial payer name.",
    )
    validate_parser.set_defaults(handler=run_validate_command)

    return parser


def parse_date_of_service(raw_value: str) -> datetime.date:
    try:
        parsed_date = datetime.date.fromisoformat(raw_value)
    except ValueError as exc:
        raise SystemExit(
            f"--date-of-service must be YYYY-MM-DD, got: {raw_value}"
        ) from exc
    return parsed_date


def run_ingest_command(settings: Settings, args: argparse.Namespace) -> None:
    """Load the curated seeds into the configured database."""
    engine = build_engine(settings)
    create_all_tables(engine)
    session_factory = build_session_factory(engine)

    # A real record-creation timestamp: this marks when the data was ingested,
    # which is ingestion metadata, not a date-of-service resolution (so using
    # the current time here does not conflict with guardrail 5).
    retrieved_at = datetime.datetime.now(datetime.timezone.utc)

    with session_factory() as session:
        summary = run_ingestion(session, retrieved_at)

    print(f"code_entries_written: {summary.code_entries_written}")
    print(f"policies_written: {summary.policies_written}")


def run_validate_command(settings: Settings, args: argparse.Namespace) -> None:
    """Stub: the extraction, retrieval, and reasoning layers are not wired yet."""
    date_of_service = parse_date_of_service(args.date_of_service)

    with open(args.note_path, "r", encoding="utf-8") as note_file:
        note_text = note_file.read()

    print(
        "medilens validate is scaffolded but the extraction, retrieval, and "
        "reasoning layers are not implemented yet.",
        file=sys.stderr,
    )
    print(f"model: {settings.model_name}")
    print(f"requested_service: {args.requested_service}")
    print(f"date_of_service: {date_of_service.isoformat()}")
    print(f"payer: {args.payer}")
    print(f"note_length_chars: {len(note_text)}")


def main() -> None:
    # Fail loudly and immediately if configuration is missing, rather than
    # letting a partially configured run proceed (CLAUDE.md section 7).
    settings = load_settings()

    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()
    args.handler(settings, args)


if __name__ == "__main__":
    main()

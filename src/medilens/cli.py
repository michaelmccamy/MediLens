"""Command-line entrypoint.

Takes a synthetic clinical note plus request metadata and will eventually
print a coding recommendation with citations and denial-risk score. The
extraction, retrieval, and reasoning layers are not wired in yet; this
stub only validates configuration and arguments so the CLI shape is settled
before that logic is built.
"""

import argparse
import datetime
import sys

from medilens.config import load_settings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medilens",
        description="Pre-claim denial prevention and documentation sufficiency check.",
    )
    parser.add_argument(
        "note_path",
        help="Path to a synthetic, de-identified clinical note text file.",
    )
    parser.add_argument(
        "--requested-service",
        required=True,
        help="Plain-language service requested, for example 'lumbar MRI'.",
    )
    parser.add_argument(
        "--date-of-service",
        required=True,
        help="Date of service in YYYY-MM-DD format. Code sets and payer "
        "policy are resolved against this date, not today.",
    )
    parser.add_argument(
        "--payer",
        required=True,
        help="Payer name, for example 'Medicare' or a commercial payer name.",
    )
    return parser


def parse_date_of_service(raw_value: str) -> datetime.date:
    try:
        parsed_date = datetime.date.fromisoformat(raw_value)
    except ValueError as exc:
        raise SystemExit(
            f"--date-of-service must be YYYY-MM-DD, got: {raw_value}"
        ) from exc
    return parsed_date


def main() -> None:
    # Fail loudly and immediately if configuration is missing, rather than
    # letting a partially configured run proceed (CLAUDE.md section 7).
    settings = load_settings()

    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()

    date_of_service = parse_date_of_service(args.date_of_service)

    with open(args.note_path, "r", encoding="utf-8") as note_file:
        note_text = note_file.read()

    print(
        "medilens CLI is scaffolded but the extraction, retrieval, and "
        "reasoning layers are not implemented yet.",
        file=sys.stderr,
    )
    print(f"model: {settings.model_name}")
    print(f"requested_service: {args.requested_service}")
    print(f"date_of_service: {date_of_service.isoformat()}")
    print(f"payer: {args.payer}")
    print(f"note_length_chars: {len(note_text)}")


if __name__ == "__main__":
    main()

"""Command-line entrypoint.

Two subcommands:

- ingest: load the curated code-set and payer-policy seeds into the database.
- validate: take a synthetic clinical note plus request metadata, run the
  reasoning pipeline (retrieve date-correct codes and policies, call the
  model, verify grounding), print the recommendation with citations and a
  denial-risk score, and write it to the append-only audit store.

The CLI stays thin: it parses arguments, loads settings, and delegates to the
orchestration, ingestion, and reasoning modules.
"""

import argparse
import datetime

from medilens.client.anthropic_client import ModelClient
from medilens.config import Settings, load_settings
from medilens.db.session import build_engine, build_session_factory, create_all_tables
from medilens.ingestion import run_ingestion
from medilens.reasoning.pipeline import (
    ValidationOutcome,
    ValidationRequest,
    content_reference,
    persist_validation,
    run_validation,
)
from medilens.reasoning.prompts import load_prompt_template


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
        help="Check a synthetic note for documentation sufficiency.",
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
    """Run the reasoning pipeline on one note and print the verified result."""
    date_of_service = parse_date_of_service(args.date_of_service)

    with open(args.note_path, "r", encoding="utf-8") as note_file:
        note_text = note_file.read()

    request = ValidationRequest(
        note_text=note_text,
        input_reference=content_reference(note_text),
        requested_service=args.requested_service,
        date_of_service=date_of_service,
        payer_name=args.payer,
        source_label=args.note_path,
    )
    prompt_template = load_prompt_template()
    model_client = ModelClient(settings)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        outcome = run_validation(session, model_client, request, prompt_template)
        created_at = datetime.datetime.now(datetime.timezone.utc)
        recommendation_id = persist_validation(session, request, outcome, created_at)

    _print_outcome(outcome, recommendation_id)


def _print_outcome(outcome: ValidationOutcome, recommendation_id: int) -> None:
    """Render a verified validation for the terminal.

    The honesty note and review framing are required on every output surface
    (CLAUDE.md guardrail 8 and guardrail 3), the CLI included.
    """
    print(
        "NOTE: This suggestion is based only on documentation currently "
        "present in the note. Do not add documentation unless it is "
        "clinically accurate. Every code below is a recommendation for a "
        "certified coder or provider to review, not a final coding decision."
    )
    print()

    verified = outcome.verified
    if len(verified.code_recommendations) == 0:
        print("No supported codes found in the documentation.")
    for recommendation in verified.code_recommendations:
        print(
            f"code: {recommendation.code} ({recommendation.code_system}) "
            f"{recommendation.description}"
        )
        print(f"  rationale: {recommendation.rationale}")
        for span in recommendation.supporting_spans:
            print(
                f'  note span [{span.start_offset}:{span.end_offset}]: "{span.text}"'
            )
        for clause in recommendation.cited_clauses:
            print(
                f"  policy clause: {clause.policy_identifier} #{clause.clause_number}: "
                f"{clause.clause_text}"
            )
        print()

    if len(verified.documentation_gaps) > 0:
        print("documentation gaps:")
        for gap in verified.documentation_gaps:
            print(f"  - {gap}")
        print()

    print(f"denial_risk_score: {verified.denial_risk_score:.2f}")
    print(f"denial_risk_rationale: {verified.denial_risk_rationale}")
    print()
    print(f"model: {outcome.model_name}")
    print(f"prompt_template_version: {outcome.prompt_template_version}")
    print(f"audit_recommendation_id: {recommendation_id}")


def main() -> None:
    # Fail loudly and immediately if configuration is missing, rather than
    # letting a partially configured run proceed (CLAUDE.md section 7).
    settings = load_settings()

    arg_parser = build_arg_parser()
    args = arg_parser.parse_args()
    args.handler(settings, args)


if __name__ == "__main__":
    main()

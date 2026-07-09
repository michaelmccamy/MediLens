"""Command-line entrypoint.

Subcommands:

- ingest: load the curated code-set and payer-policy seeds into the database.
  --policy-seed loads a custom policy YAML file instead of the bundled seed,
  which is how a curated policy authored outside the repo gets in.
- check-policies: dry-run validation of a policy YAML file. Parses the
  policy-v2 structure, lints the set for keyword ambiguity, and prints what
  would be loaded, without touching the database. Run this before ingesting
  a hand-authored file (see docs/policy-authoring.md).
- validate: take a synthetic clinical note plus request metadata, run the
  reasoning pipeline (retrieve date-correct codes and policies, call the
  model, verify grounding), print the recommendation with citations and a
  denial-risk score, and write it to the append-only audit store.
- evaluate: run the labeled synthetic evaluation set through the pipeline and
  print the section-8 metrics (code accuracy, denial precision/recall,
  citation correctness) with a denial-threshold sweep.

The CLI stays thin: it parses arguments, loads settings, and delegates to the
orchestration, ingestion, and reasoning modules.
"""

import argparse
import datetime
from pathlib import Path

from medilens.client.anthropic_client import ModelClient
from medilens.config import Settings, load_settings
from medilens.db.session import (
    build_engine,
    build_session_factory,
    create_all_tables,
    upgrade_schema,
)
from medilens.ingestion import run_ingestion
from medilens.notes.ingest import load_note_text_from_path
from medilens.phi.screening import PhiDetectedError
from medilens.reasoning.pipeline import (
    NoApplicablePolicyError,
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
    ingest_parser.add_argument(
        "--policy-seed",
        default=None,
        help="Path to a policy YAML file to load instead of the bundled "
        "seed. Author it on the policy-v2 mold (docs/policy-authoring.md) "
        "and dry-run it with check-policies first.",
    )
    ingest_parser.set_defaults(handler=run_ingest_command)

    check_parser = subparsers.add_parser(
        "check-policies",
        help="Validate a policy YAML file without touching the database.",
    )
    check_parser.add_argument(
        "policy_path",
        help="Path to the policy YAML file to parse and lint.",
    )
    check_parser.set_defaults(handler=run_check_policies_command)

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

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Run the labeled synthetic evaluation set and print metrics.",
    )
    evaluate_parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="Denial-risk threshold: a score at or above this predicts a "
        "denial. Default 0.35, the floor of the insufficient-documentation "
        "band, preferring false positives (extra review) over false "
        "negatives (missed denial risk). Use the printed sweep to tune it.",
    )
    evaluate_parser.set_defaults(handler=run_evaluate_command)

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
    upgrade_schema(engine)
    session_factory = build_session_factory(engine)

    policy_seed_path: Path | None = None
    if args.policy_seed is not None:
        policy_seed_path = Path(args.policy_seed)
        if not policy_seed_path.exists():
            raise SystemExit(f"--policy-seed file not found: {policy_seed_path}")

    # A real record-creation timestamp: this marks when the data was ingested,
    # which is ingestion metadata, not a date-of-service resolution (so using
    # the current time here does not conflict with guardrail 5).
    retrieved_at = datetime.datetime.now(datetime.timezone.utc)

    with session_factory() as session:
        summary = run_ingestion(
            session, retrieved_at, policy_seed_path=policy_seed_path
        )

    print(f"code_entries_written: {summary.code_entries_written}")
    print(f"policies_written: {summary.policies_written}")
    print(f"policies_superseded: {summary.policies_superseded}")


def run_check_policies_command(settings: Settings, args: argparse.Namespace) -> None:
    """Dry-run a policy YAML file: parse, lint, and report; never write.

    settings is unused (no database is touched) but kept for the uniform
    handler signature. Structure problems fail at parse time with a locating
    message; ambiguity problems are printed together so an author fixes them
    in one pass. Exits nonzero on any problem so this can gate a script.
    """
    from medilens.policy.ingest import parse_policy_seed_file
    from medilens.policy.lint import lint_policies

    policy_path = Path(args.policy_path)
    if not policy_path.exists():
        raise SystemExit(f"policy file not found: {policy_path}")

    parsed_policies = parse_policy_seed_file(policy_path)

    print(f"parsed {len(parsed_policies)} policies from {policy_path}")
    for policy in parsed_policies:
        clause_count = len(policy.structure.clauses)
        print(
            f"  {policy.policy_identifier}  payer={policy.payer_name!r}  "
            f"service={policy.service!r}  clauses={clause_count}"
        )

    problems = lint_policies(list(parsed_policies))
    if len(problems) > 0:
        print("\nPROBLEMS (fix before ingesting):")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)

    print(
        "\nclean: structure valid, no keyword ambiguity within this file. "
        "Note: ingest also lints against the policies already loaded, so a "
        "conflict with an existing policy is still caught at ingest time."
    )


def run_validate_command(settings: Settings, args: argparse.Namespace) -> None:
    """Run the reasoning pipeline on one note and print the verified result."""
    date_of_service = parse_date_of_service(args.date_of_service)

    note_text = load_note_text_from_path(Path(args.note_path))

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
        try:
            outcome = run_validation(session, model_client, request, prompt_template)
        except PhiDetectedError as error:
            # The note was refused before anything reached the model. Exit
            # non-zero without printing the note content.
            raise SystemExit(f"refused: {error}") from error
        except NoApplicablePolicyError as error:
            # No loaded policy governs this payer + service; refused before
            # any model call rather than validated against the wrong policy.
            raise SystemExit(f"refused: {error}") from error
        created_at = datetime.datetime.now(datetime.timezone.utc)
        recommendation_id = persist_validation(session, request, outcome, created_at)

    _print_outcome(outcome, recommendation_id)


def run_evaluate_command(settings: Settings, args: argparse.Namespace) -> None:
    """Run the labeled evaluation set through the pipeline and print metrics.

    Makes one model call per case. The gold labels are synthetic placeholders,
    so the printed numbers are a harness demonstration, not a real accuracy
    claim until the labels are reviewed by a certified coder (CLAUDE.md
    section 8). Reads only; writes no audit records.
    """
    from medilens.eval.dataset import load_default_cases
    from medilens.eval.runner import evaluate, format_report

    cases = load_default_cases()
    prompt_template = load_prompt_template()
    model_client = ModelClient(settings)

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        report = evaluate(
            session, model_client, prompt_template, cases,
            threshold=args.threshold,
        )
    print(format_report(report))


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
    assessment = outcome.assessment

    print(f"coverage determination: {assessment.determination}")
    if assessment.determination == "manual_review":
        print(
        "  NEEDS HUMAN REVIEW: at least one required clause depends on "
        "information this tool cannot assess (for example claims history). "
        "This is not a denial prediction."
        )
    print(f"denial_risk_score (computed from clause statuses): "
          f"{assessment.denial_risk_score:.2f}")
    print(f"  {assessment.determination_rationale}")
    print()

    print("clause results:")
    for clause_result in assessment.clause_results:
        print(
            f"  [{clause_result.status}] {clause_result.policy_identifier}."
            f"{clause_result.clause_id} (decided by {clause_result.decided_by}): "
            f"{clause_result.detail}"
        )
        for span in clause_result.evidence:
            print(
                f'    evidence [{span.start_offset}:{span.end_offset}]: "{span.text}"'
            )
    print()

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
        print()

    if len(verified.documentation_gaps) > 0:
        print("documentation gaps:")
        for gap in verified.documentation_gaps:
            print(f"  - {gap}")
        print()

    if verified.coverage_rationale:
        print(
            "model narrative (prose only; the determination and score above "
            "are computed from clause statuses, not from this text):"
        )
        print(f"  {verified.coverage_rationale}")
        print()

    if len(verified.rejections) > 0:
        # Surface, never hide, what the model produced that failed a check.
        print("dropped or downgraded by verification:")
        for rejection in verified.rejections:
            print(f"  - {rejection}")
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

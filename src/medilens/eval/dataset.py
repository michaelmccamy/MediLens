"""Load labeled evaluation cases and their synthetic notes.

A case pairs a synthetic note with a request (service, date, payer) and gold
labels (expected codes, expected denial, or an expected refusal). Notes are
normalized on load with the same normalizer the CLI and UI apply, so the note
the harness scores is byte-identical to what production would feed the model.
"""

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from medilens.notes.ingest import normalize_note_text

_CASES_DIR = Path(__file__).parent / "cases"
_NOTES_DIR = Path(__file__).parent / "notes"
_DEFAULT_CASE_FILE = _CASES_DIR / "ortho_pain_v1.yaml"


@dataclass(frozen=True)
class EvalCase:
    """One labeled evaluation case.

    note_text is already normalized. expected_denied is None for refusal cases
    (denial is not defined when the system refuses before assessing coverage).
    """

    case_id: str
    note_text: str
    requested_service: str
    date_of_service: datetime.date
    payer_name: str
    expected_codes: frozenset[str]
    expected_denied: bool | None
    expect_refusal: bool
    label_rationale: str


def _require(mapping: dict, key: str, context: str):
    if key not in mapping:
        raise ValueError(f"evaluation case {context} is missing required key {key!r}")
    return mapping[key]


def parse_cases_file(case_path: Path, notes_dir: Path) -> list[EvalCase]:
    """Parse a cases YAML file, resolving and normalizing each note.

    Fails loudly on a missing note file or a malformed case rather than
    silently skipping it: an eval set that quietly drops cases would report a
    metric over fewer cases than intended (CLAUDE.md section 7).
    """
    with case_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)

    raw_cases = _require(document, "cases", str(case_path))
    if not isinstance(raw_cases, list) or len(raw_cases) == 0:
        raise ValueError(f"evaluation file {case_path} has no cases")

    cases: list[EvalCase] = []
    for raw_case in raw_cases:
        case_id = _require(raw_case, "id", str(case_path))
        note_file = _require(raw_case, "note_file", case_id)
        note_path = notes_dir / note_file
        if not note_path.exists():
            raise ValueError(
                f"evaluation case {case_id!r} references missing note file "
                f"{note_path}"
            )
        note_text = normalize_note_text(note_path.read_text(encoding="utf-8"))

        requested_service = _require(raw_case, "requested_service", case_id)
        payer_name = _require(raw_case, "payer", case_id)

        raw_date = _require(raw_case, "date_of_service", case_id)
        if isinstance(raw_date, datetime.date):
            date_of_service = raw_date
        else:
            date_of_service = datetime.date.fromisoformat(str(raw_date))

        expected_codes = frozenset(raw_case.get("expected_codes", []) or [])
        expected_denied = raw_case.get("expected_denied", None)
        expect_refusal = bool(raw_case.get("expect_refusal", False))
        label_rationale = str(raw_case.get("label_rationale", "")).strip()

        cases.append(
            EvalCase(
                case_id=case_id,
                note_text=note_text,
                requested_service=requested_service,
                date_of_service=date_of_service,
                payer_name=payer_name,
                expected_codes=expected_codes,
                expected_denied=expected_denied,
                expect_refusal=expect_refusal,
                label_rationale=label_rationale,
            )
        )
    return cases


def load_default_cases() -> list[EvalCase]:
    """Load the packaged ortho/pain evaluation set."""
    return parse_cases_file(_DEFAULT_CASE_FILE, _NOTES_DIR)

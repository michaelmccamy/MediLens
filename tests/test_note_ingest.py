"""Tests for note ingestion and normalization."""

from pathlib import Path

import pytest

from medilens.notes.ingest import (
    extract_note_text,
    load_and_normalize_upload,
    normalize_note_text,
)
from medilens.phi.screening import assert_no_blocking_phi, screen_for_phi

EPIC_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "synthetic_notes"
    / "epic_style_lumbar_mri.txt"
)


def test_normalizes_line_endings() -> None:
    result = normalize_note_text("line one\r\nline two\rline three")

    assert "\r" not in result
    assert "line one\nline two\nline three" in result


def test_collapses_blank_line_runs() -> None:
    result = normalize_note_text("a\n\n\n\n\nb")

    assert result == "a\n\nb\n"


def test_strips_trailing_line_whitespace() -> None:
    result = normalize_note_text("Chief complaint:   \nLow back pain.   ")

    assert "Chief complaint:\nLow back pain.\n" == result


def test_folds_unicode_punctuation() -> None:
    # Curly quotes, em dash, ellipsis, non-breaking space. Built from code
    # points so this test file contains no literal special characters.
    left_quote = chr(0x201C)
    right_quote = chr(0x201D)
    em_dash = chr(0x2014)
    ellipsis = chr(0x2026)
    nbsp = chr(0x00A0)
    raw = f"Pain{nbsp}is {left_quote}sharp{right_quote}{em_dash}worse{ellipsis}"
    result = normalize_note_text(raw)

    assert '"sharp"' in result
    assert "-worse" in result
    assert "worse..." in result
    assert nbsp not in result


def test_normalization_is_idempotent() -> None:
    raw = "a\r\n\n\n b  \n“q”"
    once = normalize_note_text(raw)
    twice = normalize_note_text(once)

    assert once == twice


def test_extract_plain_text() -> None:
    text = extract_note_text("note.txt", b"Low back pain.")

    assert text == "Low back pain."


def test_extract_rtf() -> None:
    rtf = (
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times New Roman;}}"
        r"\f0\fs24 Low back pain radiating to left leg.\par}"
    )
    text = extract_note_text("dictation.rtf", rtf.encode("utf-8"))

    assert "Low back pain radiating to left leg." in text


def test_extract_rejects_unsupported_format() -> None:
    with pytest.raises(ValueError, match="unsupported note format"):
        extract_note_text("scan.pdf", b"%PDF-1.7 ...")


def test_load_and_normalize_upload_combines_steps() -> None:
    result = load_and_normalize_upload("note.txt", b"Chief complaint:  \r\n\r\n\r\nPain.")

    assert result == "Chief complaint:\n\nPain.\n"


def test_epic_style_fixture_passes_phi_gate() -> None:
    note = normalize_note_text(EPIC_FIXTURE_PATH.read_text(encoding="utf-8"))

    # The realistic note must not trip the blocking gate. Its MRN and DOB are
    # detected as medium (non-blocking), which exercises that path.
    assert_no_blocking_phi(note)
    categories = {finding.category for finding in screen_for_phi(note)}
    assert "labeled_mrn" in categories
    assert "labeled_dob" in categories


def test_epic_style_fixture_has_no_high_severity_phi() -> None:
    from medilens.phi.screening import PhiSeverity

    note = normalize_note_text(EPIC_FIXTURE_PATH.read_text(encoding="utf-8"))
    high = [
        finding
        for finding in screen_for_phi(note)
        if finding.severity is PhiSeverity.HIGH
    ]

    assert high == []

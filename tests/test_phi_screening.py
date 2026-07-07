"""Tests for the PHI screening gate.

Covers what the screen must catch (structured identifiers), what it must not
false-positive on (ordinary clinical numbers and ISO dates of service), that
the bundled synthetic fixture passes, and that findings and errors never carry
the matched value (guardrail 6).
"""

from pathlib import Path

import pytest

from medilens.phi.screening import (
    PhiDetectedError,
    PhiSeverity,
    assert_no_blocking_phi,
    screen_for_phi,
)

FIXTURE_NOTE_PATH = (
    Path(__file__).parent / "fixtures" / "synthetic_notes" / "lumbar_mri_example.txt"
)


def _categories(text: str) -> set[str]:
    return {finding.category for finding in screen_for_phi(text)}


def test_detects_ssn() -> None:
    assert "ssn" in _categories("Patient SSN 123-45-6789 on file.")


def test_detects_email() -> None:
    assert "email" in _categories("Contact jane.doe@example.com for records.")


def test_detects_phone() -> None:
    assert "phone" in _categories("Call the patient at 415-555-0132 to confirm.")


def test_detects_ip_address() -> None:
    assert "ip_address" in _categories("Logged from 192.168.1.55 during intake.")


def test_ssn_email_phone_ip_are_high_severity() -> None:
    findings = screen_for_phi(
        "SSN 123-45-6789, email a@b.com, phone 415-555-0132, ip 10.0.0.1"
    )
    high = [f for f in findings if f.severity is PhiSeverity.HIGH]
    assert len(high) >= 4


def test_does_not_flag_clinical_numbers() -> None:
    # Strength grades, angles, durations, and ISO dates must not read as PHI.
    clinical = (
        "Strength 4/5 left EHL. Positive SLR at 40 degrees. 8 weeks duration. "
        "Date of service 2026-06-01. Reflexes 2+ symmetric."
    )
    findings = screen_for_phi(clinical)
    high = [f for f in findings if f.severity is PhiSeverity.HIGH]
    assert high == []


def test_synthetic_fixture_passes_the_gate() -> None:
    note = FIXTURE_NOTE_PATH.read_text(encoding="utf-8")

    # The bundled synthetic note must not trip the blocking gate, or the whole
    # test and demo workflow would be blocked.
    assert_no_blocking_phi(note)


def test_assert_blocks_on_high_severity() -> None:
    with pytest.raises(PhiDetectedError):
        assert_no_blocking_phi("Reason for visit noted. SSN 123-45-6789.")


def test_error_message_excludes_the_value() -> None:
    secret_ssn = "123-45-6789"
    try:
        assert_no_blocking_phi(f"SSN {secret_ssn} recorded.")
    except PhiDetectedError as error:
        # The category summary is fine; the actual identifier must not appear.
        assert "ssn" in str(error)
        assert secret_ssn not in str(error)
    else:
        pytest.fail("expected PhiDetectedError")


def test_findings_carry_offsets_not_values() -> None:
    text = "Email a@b.com here."
    findings = screen_for_phi(text)

    assert len(findings) >= 1
    finding = findings[0]
    # A PhiFinding exposes location and category, never the matched string.
    assert not hasattr(finding, "value")
    assert text[finding.start_offset : finding.end_offset] == "a@b.com"


def test_labeled_dob_is_medium_not_blocking() -> None:
    findings = screen_for_phi("DOB: 1975-01-01")
    dob = [f for f in findings if f.category == "labeled_dob"]
    assert len(dob) == 1
    assert dob[0].severity is PhiSeverity.MEDIUM
    # Medium findings alone do not block.
    assert_no_blocking_phi("DOB: 1975-01-01")

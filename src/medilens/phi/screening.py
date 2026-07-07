"""PHI screening: a hard stop before any note text reaches the model endpoint.

CLAUDE.md guardrail 6 and section 2 are explicit: real PHI must not be sent to
a model endpoint that is not covered by a signed BAA, and the BAA-covered
deployment path is deferred. This system runs on the standard first-party API
today, so any text that looks like it carries real PHI must be refused before
it leaves the process.

Scope and honesty about limits. This is a screening safety-net, not a
compliant de-identifier. It reliably catches high-confidence STRUCTURED
identifiers (Social Security numbers, email addresses, phone numbers, IP
addresses) that essentially never survive proper de-identification, so their
presence strongly indicates a real note was pasted in by mistake. It does NOT
reliably catch free-text patient names, which need trained named-entity
recognition and are imperfect even then. Passing this screen does not mean a
note is de-identified; failing it means something clearly identifying is
present. Proper de-identification and the BAA path remain prerequisites for
real PHI (deferred, section 2).

Findings never include the matched value, only its category and character
offsets, so screening a note does not itself write PHI into logs or errors
(guardrail 6).
"""

import enum
import re
from dataclasses import dataclass


class PhiSeverity(enum.Enum):
    """HIGH findings block the request; MEDIUM findings are reported only."""

    HIGH = "high"
    MEDIUM = "medium"


class PhiDetectedError(Exception):
    """Blocking PHI was detected; the text must not be sent to the model."""


@dataclass(frozen=True)
class PhiFinding:
    """One detected identifier: its category and location, never its value."""

    category: str
    severity: PhiSeverity
    start_offset: int
    end_offset: int


# Each detector is (category, severity, compiled pattern). Patterns target
# structure specific enough to avoid firing on ordinary clinical numbers such
# as "4/5 strength", "40 degrees", or ISO dates like "2026-06-01".
_DETECTORS: list[tuple[str, PhiSeverity, re.Pattern[str]]] = [
    (
        "ssn",
        PhiSeverity.HIGH,
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "email",
        PhiSeverity.HIGH,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "phone",
        PhiSeverity.HIGH,
        # US phone in 3-3-4 grouping, optional country code and separators.
        re.compile(
            r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
        ),
    ),
    (
        "ip_address",
        PhiSeverity.HIGH,
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ),
    (
        "labeled_mrn",
        PhiSeverity.MEDIUM,
        re.compile(r"\bMRN\b[:\s#]*\w+", re.IGNORECASE),
    ),
    (
        "labeled_dob",
        PhiSeverity.MEDIUM,
        re.compile(
            r"\b(?:DOB|date of birth)\b[:\s]*\d{1,4}[-/]\d{1,2}[-/]\d{1,4}",
            re.IGNORECASE,
        ),
    ),
]


def screen_for_phi(text: str) -> list[PhiFinding]:
    """Return all PHI findings in the text, ordered by position.

    Detectors are checked independently, so a single span can produce more than
    one finding (for example an email that also looks like something else).
    That over-reporting is deliberate: the screen errs toward flagging.
    """
    findings: list[PhiFinding] = []
    for category, severity, pattern in _DETECTORS:
        for match in pattern.finditer(text):
            findings.append(
                PhiFinding(
                    category=category,
                    severity=severity,
                    start_offset=match.start(),
                    end_offset=match.end(),
                )
            )
    findings.sort(key=lambda finding: finding.start_offset)
    return findings


def assert_no_blocking_phi(text: str) -> None:
    """Refuse text carrying high-confidence PHI before it reaches the model.

    Raises PhiDetectedError if any HIGH-severity identifier is present. This is
    the non-BAA safety stop; when a BAA-covered path exists this policy changes
    from refuse-to-send to send-to-the-covered-endpoint, but the screen itself
    stays useful. The error names how many of each category were found, never
    the values (guardrail 6).
    """
    findings = screen_for_phi(text)
    blocking = []
    for finding in findings:
        if finding.severity is PhiSeverity.HIGH:
            blocking.append(finding)
    if len(blocking) == 0:
        return

    counts: dict[str, int] = {}
    for finding in blocking:
        counts[finding.category] = counts.get(finding.category, 0) + 1
    summary = ", ".join(
        f"{category} x{count}" for category, count in sorted(counts.items())
    )
    raise PhiDetectedError(
        "text appears to contain real PHI (" + summary + ") and was not sent "
        "to the model. This deployment is not BAA covered, so only synthetic "
        "or de-identified notes may be processed (CLAUDE.md guardrail 6). "
        "Values are withheld from this message to keep PHI out of logs."
    )

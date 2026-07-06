"""Date-resolved retrieval from the knowledge store.

CLAUDE.md guardrail 5 requires every code-set lookup to resolve against the
date of service, not today. These functions never read the current clock: the
date of service is always an explicit argument, so a claim for a past service
resolves against the code set that was in force then, not the current one.

Validity rule: a code is in force on a date when its effective_start is on or
before that date and its effective_end is null (still active) or on or after
that date. effective_end is treated as the last date the code is valid
(inclusive), matching how annual code-set deletions are dated.
"""

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medilens.db.models import CodeSetEntry


def _in_force_condition(code_system: str, date_of_service: datetime.date):
    """Build the SQLAlchemy filter for codes in force on the date of service.

    Kept as one helper so the single-code and list queries cannot drift in how
    they define validity.
    """
    return (
        (CodeSetEntry.code_system == code_system)
        & (CodeSetEntry.effective_start <= date_of_service)
        & (
            (CodeSetEntry.effective_end.is_(None))
            | (CodeSetEntry.effective_end >= date_of_service)
        )
    )


def find_code_at_date(
    session: Session,
    code_system: str,
    code: str,
    date_of_service: datetime.date,
) -> CodeSetEntry | None:
    """Return the entry for one code in force on the date of service, or None.

    None means the code did not exist or was not in force on that date. The
    caller must treat that as "no supporting code found" rather than inferring
    one (CLAUDE.md guardrail 4).
    """
    query = select(CodeSetEntry).where(
        _in_force_condition(code_system, date_of_service)
        & (CodeSetEntry.code == code)
    )
    result_row = session.execute(query).scalar_one_or_none()
    return result_row


def list_codes_at_date(
    session: Session,
    code_system: str,
    date_of_service: datetime.date,
) -> list[CodeSetEntry]:
    """Return all codes in a system that are in force on the date of service.

    Used to feed the reasoning layer the date-correct candidate set for a
    request instead of the whole table.
    """
    query = (
        select(CodeSetEntry)
        .where(_in_force_condition(code_system, date_of_service))
        .order_by(CodeSetEntry.code)
    )
    result_rows = session.execute(query).scalars().all()
    return list(result_rows)

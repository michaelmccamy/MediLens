"""Date-resolved retrieval from the payer-policy store.

CLAUDE.md guardrail 5 requires policy lookups to resolve against the date of
service, not today, so a claim for a past service is checked against the policy
that was in force then. The date of service is always an explicit argument;
these functions never read the current clock. Validity uses the shared
effective-date window (see medilens.date_resolution): effective_end is the last
date a policy is in force (inclusive).
"""

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medilens.date_resolution import effective_date_window
from medilens.db.models import PayerPolicy


def find_policy_at_date(
    session: Session,
    payer_name: str,
    policy_identifier: str,
    date_of_service: datetime.date,
) -> PayerPolicy | None:
    """Return one payer's policy in force on the date of service, or None.

    None means the payer had no such policy in force on that date. The caller
    must treat that as "no governing policy found" rather than assuming
    coverage or non-coverage (CLAUDE.md guardrail 4).
    """
    query = select(PayerPolicy).where(
        (PayerPolicy.payer_name == payer_name)
        & (PayerPolicy.policy_identifier == policy_identifier)
        & effective_date_window(PayerPolicy, date_of_service)
    )
    result_row = session.execute(query).scalar_one_or_none()
    return result_row


def list_policies_for_payer_at_date(
    session: Session,
    payer_name: str,
    specialty: str,
    date_of_service: datetime.date,
) -> list[PayerPolicy]:
    """Return a payer's policies for a specialty in force on the date of service.

    Feeds the reasoning layer the date-correct policy set for a request instead
    of the whole table, scoped to the requesting payer and specialty.
    """
    query = (
        select(PayerPolicy)
        .where(
            (PayerPolicy.payer_name == payer_name)
            & (PayerPolicy.specialty == specialty)
            & effective_date_window(PayerPolicy, date_of_service)
        )
        .order_by(PayerPolicy.policy_identifier)
    )
    result_rows = session.execute(query).scalars().all()
    return list(result_rows)

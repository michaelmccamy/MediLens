"""Date-resolved retrieval from the payer-policy store.

CLAUDE.md guardrail 5 requires policy lookups to resolve against the date of
service, not today, so a claim for a past service is checked against the policy
that was in force then. The date of service is always an explicit argument;
these functions never read the current clock. Validity uses the shared
effective-date window (see medilens.date_resolution): effective_end is the last
date a policy is in force (inclusive).
"""

import datetime
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from medilens.date_resolution import effective_date_window
from medilens.db.models import PayerPolicy


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of a phrase, for service matching."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def service_matches(requested_service: str, service_keywords: str) -> bool:
    """Decide whether a requested service is governed by a policy.

    service_keywords is the policy's curated, comma-joined keyword list. The
    request matches when every token of any one keyword phrase appears in the
    request. This is deterministic and explainable (the matching phrase can be
    named in an audit), which matters more here than linguistic cleverness: a
    policy must never be applied to a service it does not govern, and a miss
    is surfaced loudly rather than silently mismatched.
    """
    requested_tokens = _tokens(requested_service)
    if len(requested_tokens) == 0:
        return False
    for keyword_phrase in service_keywords.split(","):
        keyword_tokens = _tokens(keyword_phrase)
        if len(keyword_tokens) == 0:
            continue
        if keyword_tokens <= requested_tokens:
            return True
    return False


def find_policy_at_date(
    session: Session,
    payer_name: str,
    policy_identifier: str,
    date_of_service: datetime.date,
) -> PayerPolicy | None:
    """Return one payer's policy in force on the date of service, or None.

    None means the payer had no such policy in force on that date. The caller
    must treat that as "no governing policy found" rather than assuming
    coverage or non-coverage (CLAUDE.md guardrail 4). Only the current version
    is consulted: superseded rows are audit history, and a stale version
    answering a live query would resurrect exactly the content a re-ingest
    replaced.
    """
    query = select(PayerPolicy).where(
        (PayerPolicy.payer_name == payer_name)
        & (PayerPolicy.policy_identifier == policy_identifier)
        & (PayerPolicy.superseded_at.is_(None))
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
    of the whole table, scoped to the requesting payer and specialty. Only
    current versions are returned; superseded rows are audit history and must
    never govern retrieval (in particular their stale service keywords must
    never match a request).
    """
    query = (
        select(PayerPolicy)
        .where(
            (PayerPolicy.payer_name == payer_name)
            & (PayerPolicy.specialty == specialty)
            & (PayerPolicy.superseded_at.is_(None))
            & effective_date_window(PayerPolicy, date_of_service)
        )
        .order_by(PayerPolicy.policy_identifier)
    )
    result_rows = session.execute(query).scalars().all()
    return list(result_rows)


def list_policies_for_service_at_date(
    session: Session,
    payer_name: str,
    specialty: str,
    requested_service: str,
    date_of_service: datetime.date,
) -> list[PayerPolicy]:
    """Return the payer's in-force policies that govern the requested service.

    The service filter runs in Python over the payer's in-force policy set
    (small by construction: curated per payer and specialty), because keyword
    matching is not expressible as a portable SQL predicate. Policies without
    service keywords (pre-service-matching rows) never match; they are legacy
    versions kept for audit history.
    """
    all_policies = list_policies_for_payer_at_date(
        session, payer_name, specialty, date_of_service
    )
    matching_policies: list[PayerPolicy] = []
    for policy in all_policies:
        if service_matches(requested_service, policy.service_keywords):
            matching_policies.append(policy)
    return matching_policies

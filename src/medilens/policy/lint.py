"""Authoring-time checks for payer policy records.

Structure validation (parse_policy_structure) proves a single policy is
well-formed. These checks prove a SET of policies is unambiguous, which
structure validation cannot see. They exist because of a real bug: the knee
and hip injection policies both carried the joint-less keyword "major joint
injection", so each injection request matched both policies and was judged
against the other joint's covered-diagnosis set. That is a data defect no
single-policy check can catch.

The canonical request strings are the policy service labels themselves (the
review UI derives its dropdown from them), so ambiguity is defined against
them:

- A policy whose own service label does not match its own keywords is
  unreachable from its canonical label.
- A policy whose service label matches another policy's keywords (same payer)
  is ambiguous: one request would be judged against two different services.
- Two batch entries for the same payer and identifier make supersession
  order-dependent within one run.

Used by the check-policies CLI command for fast authoring feedback, and
enforced inside ingest_policies so an ambiguous set can never load.
"""

from typing import Protocol

from medilens.policy.retrieval import service_matches


class PolicyLike(Protocol):
    """The four fields lint needs; satisfied by ParsedPolicy and PayerPolicy."""

    payer_name: str
    policy_identifier: str
    service: str
    service_keywords: str


def lint_policies(policies: list[PolicyLike]) -> list[str]:
    """Return human-readable problems for an ambiguous policy set.

    An empty list means the set is clean. Callers decide severity: the CLI
    check command prints problems, ingest raises on them.
    """
    problems: list[str] = []

    seen_identifiers: set[tuple[str, str]] = set()
    for policy in policies:
        identity = (policy.payer_name, policy.policy_identifier)
        if identity in seen_identifiers:
            problems.append(
                f"duplicate policy entry for payer {policy.payer_name!r} "
                f"identifier {policy.policy_identifier!r}: supersession "
                "within one ingest run would depend on file order"
            )
        seen_identifiers.add(identity)

    for policy in policies:
        if not service_matches(policy.service, policy.service_keywords):
            problems.append(
                f"policy {policy.policy_identifier!r} ({policy.payer_name}): "
                f"its own service label {policy.service!r} does not match its "
                f"keywords {policy.service_keywords!r}; the policy is "
                "unreachable from its canonical service label"
            )

    for policy in policies:
        for other in policies:
            if other is policy:
                continue
            if other.payer_name != policy.payer_name:
                continue
            if other.policy_identifier == policy.policy_identifier:
                continue
            if other.service == policy.service:
                # The same service carried by two policy identifiers for one
                # payer (for example a replacement policy number) is a curation
                # choice, not an ambiguity between different services.
                continue
            if service_matches(policy.service, other.service_keywords):
                problems.append(
                    f"ambiguous keywords: the service label of "
                    f"{policy.policy_identifier!r} ({policy.service!r}) also "
                    f"matches the keywords of {other.policy_identifier!r} "
                    f"({other.service_keywords!r}) for payer "
                    f"{policy.payer_name!r}; one request would be judged "
                    "against two different services. Add a disambiguating "
                    "token (for example the joint or the body region) to "
                    "every keyword phrase"
                )

    return problems

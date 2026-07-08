"""Structured policy records: the policy-v2 mold (docs/policy-schema.md).

A v2 policy is not free text: it is a list of clauses, each with a stable
clause_id, an evaluation type, and (depending on the type) a deterministic
rule and/or a model-judgment question, plus the structured clinical facts the
rules consume. This module owns that shape: parsing and validating it from
seed YAML, serializing it canonically for storage and hashing, and rendering a
deterministic human-readable text block for display and for the model's
context.

Validation is strict and loud (CLAUDE.md section 7): a malformed policy is a
configuration error caught at ingest time, never a runtime surprise inside the
evaluator.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Evaluation types (docs/policy-schema.md section 4).
EVALUATION_DETERMINISTIC = "deterministic"
EVALUATION_MODEL_JUDGED = "model_judged"
EVALUATION_HYBRID = "hybrid"
EVALUATION_MANUAL_REVIEW = "manual_review"
EVALUATION_TYPES = frozenset(
    {
        EVALUATION_DETERMINISTIC,
        EVALUATION_MODEL_JUDGED,
        EVALUATION_HYBRID,
        EVALUATION_MANUAL_REVIEW,
    }
)

# Fact value types and sources (section 6).
FACT_TYPES = frozenset({"duration", "count", "date", "boolean"})
FACT_SOURCE_NOTE = "note"
FACT_SOURCE_REQUEST = "request"
FACT_SOURCE_HISTORY = "history"
FACT_SOURCES = frozenset({FACT_SOURCE_NOTE, FACT_SOURCE_REQUEST, FACT_SOURCE_HISTORY})

# Deterministic rule operators (section 5).
RULE_OPERATORS = frozenset(
    {
        "min_duration",
        "max_duration",
        "min_count",
        "max_count",
        "frequency_limit",
        "date_within",
        "code_in_set",
        "boolean_true",
        "boolean_false",
    }
)

# Operators whose rule must reference a declared fact via a "fact" param.
_FACT_OPERATORS = frozenset(
    {
        "min_duration",
        "max_duration",
        "min_count",
        "max_count",
        "frequency_limit",
        "date_within",
        "boolean_true",
        "boolean_false",
    }
)

SCHEMA_VERSION_V2 = "policy-v2"


@dataclass(frozen=True)
class FactSpec:
    """One structured clinical fact a policy's rules consume.

    source determines the fail-closed behavior when the fact is missing
    (section 8): note -> insufficient_documentation, history -> manual_review.
    request-sourced values come from request metadata and are always present.
    """

    key: str
    type: str
    source: str
    unit: str | None = None
    description: str = ""


@dataclass(frozen=True)
class RuleSpec:
    """One deterministic rule: an operator plus its parameters."""

    op: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgmentSpec:
    """The question a model judgment must answer, with evidence."""

    question: str
    requires_evidence: bool = True


@dataclass(frozen=True)
class ClauseSpec:
    """One policy clause (section 3). clause_id is stable, never positional.

    bypasses declares a policy-level override: when THIS clause resolves
    satisfied with verified evidence, every clause listed in bypasses becomes
    not_applicable. Membership is explicit policy data (for example a red-flag
    clause bypassing the entire gating prerequisite set); the engine never
    special-cases any clause name, and the model can never trigger a bypass
    directly.
    """

    clause_id: str
    title: str
    text: str
    evaluation: str
    required: bool
    bypasses: tuple[str, ...] = ()
    rule: RuleSpec | None = None
    judgment: JudgmentSpec | None = None
    source_ref: str = ""

    @property
    def needs_judgment(self) -> bool:
        return self.evaluation in (EVALUATION_MODEL_JUDGED, EVALUATION_HYBRID)

    @property
    def needs_rule(self) -> bool:
        return self.evaluation in (EVALUATION_DETERMINISTIC, EVALUATION_HYBRID)


@dataclass(frozen=True)
class PolicyStructure:
    """The structured content of one policy-v2 record."""

    schema_version: str
    version: int
    source_type: str
    source_authoritative: bool
    source_citation: str
    required_facts: tuple[FactSpec, ...]
    clauses: tuple[ClauseSpec, ...]

    def fact_specs_by_key(self) -> dict[str, FactSpec]:
        specs: dict[str, FactSpec] = {}
        for spec in self.required_facts:
            specs[spec.key] = spec
        return specs

    def clause_by_id(self, clause_id: str) -> ClauseSpec | None:
        for clause in self.clauses:
            if clause.clause_id == clause_id:
                return clause
        return None


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"policy structure {context} is missing required key {key!r}")
    return mapping[key]


def parse_policy_structure(raw: dict[str, Any], context: str) -> PolicyStructure:
    """Parse and validate a structured policy from its seed mapping.

    Every structural error is raised with the offending context, so a bad seed
    fails the ingest run instead of producing an evaluator surprise later.
    """
    schema_version = _require(raw, "schema_version", context)
    if schema_version != SCHEMA_VERSION_V2:
        raise ValueError(
            f"policy structure {context} has unsupported schema_version "
            f"{schema_version!r}; expected {SCHEMA_VERSION_V2!r}"
        )
    version = int(_require(raw, "version", context))

    source = _require(raw, "source", context)
    source_type = _require(source, "type", f"{context}.source")
    source_authoritative = bool(_require(source, "authoritative", f"{context}.source"))
    source_citation = str(_require(source, "citation", f"{context}.source"))
    if source_type not in {"synthetic", "lcd", "ncd", "commercial_policy"}:
        raise ValueError(
            f"policy structure {context} has unknown source type {source_type!r}"
        )
    if source_type == "synthetic" and source_authoritative:
        raise ValueError(
            f"policy structure {context} marks a synthetic source as "
            "authoritative; synthetic policies are never authoritative"
        )

    fact_specs: list[FactSpec] = []
    fact_keys: set[str] = set()
    for raw_fact in raw.get("required_facts", []) or []:
        fact_context = f"{context}.required_facts"
        key = _require(raw_fact, "key", fact_context)
        fact_type = _require(raw_fact, "type", f"{fact_context}.{key}")
        fact_source = _require(raw_fact, "source", f"{fact_context}.{key}")
        if fact_type not in FACT_TYPES:
            raise ValueError(
                f"fact {key!r} in {context} has unknown type {fact_type!r}"
            )
        if fact_source not in FACT_SOURCES:
            raise ValueError(
                f"fact {key!r} in {context} has unknown source {fact_source!r}"
            )
        if key in fact_keys:
            raise ValueError(f"fact key {key!r} is duplicated in {context}")
        fact_keys.add(key)
        fact_specs.append(
            FactSpec(
                key=key,
                type=fact_type,
                source=fact_source,
                unit=raw_fact.get("unit"),
                description=str(raw_fact.get("description", "")),
            )
        )

    raw_clauses = _require(raw, "clauses", context)
    if not isinstance(raw_clauses, list) or len(raw_clauses) == 0:
        raise ValueError(f"policy structure {context} has no clauses")

    clauses: list[ClauseSpec] = []
    clause_ids: set[str] = set()
    for raw_clause in raw_clauses:
        clause_id = _require(raw_clause, "clause_id", context)
        clause_context = f"{context}.clauses.{clause_id}"
        if clause_id in clause_ids:
            raise ValueError(f"clause_id {clause_id!r} is duplicated in {context}")
        clause_ids.add(clause_id)

        evaluation = _require(raw_clause, "evaluation", clause_context)
        if evaluation not in EVALUATION_TYPES:
            raise ValueError(
                f"clause {clause_id!r} in {context} has unknown evaluation "
                f"type {evaluation!r}"
            )

        rule_spec: RuleSpec | None = None
        raw_rule = raw_clause.get("rule")
        if raw_rule is not None:
            op = _require(raw_rule, "op", clause_context)
            if op not in RULE_OPERATORS:
                raise ValueError(
                    f"clause {clause_id!r} in {context} uses unknown rule "
                    f"operator {op!r}"
                )
            params: dict[str, Any] = {}
            for param_key, param_value in raw_rule.items():
                if param_key != "op":
                    params[param_key] = param_value
            if op in _FACT_OPERATORS:
                fact_ref = params.get("fact")
                if fact_ref not in fact_keys:
                    raise ValueError(
                        f"clause {clause_id!r} in {context} rule references "
                        f"fact {fact_ref!r} which is not declared in "
                        "required_facts"
                    )
            rule_spec = RuleSpec(op=op, params=params)

        judgment_spec: JudgmentSpec | None = None
        raw_judgment = raw_clause.get("judgment")
        if raw_judgment is not None:
            judgment_spec = JudgmentSpec(
                question=str(_require(raw_judgment, "question", clause_context)),
                requires_evidence=bool(raw_judgment.get("requires_evidence", True)),
            )

        # Evaluation type dictates which parts must be present (section 4).
        if evaluation == EVALUATION_DETERMINISTIC and rule_spec is None:
            raise ValueError(
                f"deterministic clause {clause_id!r} in {context} has no rule"
            )
        if evaluation == EVALUATION_MODEL_JUDGED and judgment_spec is None:
            raise ValueError(
                f"model_judged clause {clause_id!r} in {context} has no judgment"
            )
        if evaluation == EVALUATION_HYBRID and (
            rule_spec is None or judgment_spec is None
        ):
            raise ValueError(
                f"hybrid clause {clause_id!r} in {context} needs both a rule "
                "and a judgment"
            )
        if evaluation == EVALUATION_MANUAL_REVIEW and (
            rule_spec is not None or judgment_spec is not None
        ):
            raise ValueError(
                f"manual_review clause {clause_id!r} in {context} must not "
                "carry a rule or judgment; it always defers to a human"
            )

        bypasses_raw = raw_clause.get("bypasses", []) or []
        bypasses: list[str] = []
        for bypassed_id in bypasses_raw:
            bypasses.append(str(bypassed_id))

        clauses.append(
            ClauseSpec(
                clause_id=clause_id,
                title=str(_require(raw_clause, "title", clause_context)),
                text=str(_require(raw_clause, "text", clause_context)).strip(),
                evaluation=evaluation,
                required=bool(_require(raw_clause, "required", clause_context)),
                bypasses=tuple(bypasses),
                rule=rule_spec,
                judgment=judgment_spec,
                source_ref=str(raw_clause.get("source_ref", "")),
            )
        )

    # Bypass references must point at real, other clauses in this policy.
    for clause in clauses:
        for bypassed_id in clause.bypasses:
            if bypassed_id not in clause_ids:
                raise ValueError(
                    f"clause {clause.clause_id!r} in {context} bypasses "
                    f"{bypassed_id!r} which does not exist in this policy"
                )
            if bypassed_id == clause.clause_id:
                raise ValueError(
                    f"clause {clause.clause_id!r} in {context} cannot bypass "
                    "itself"
                )

    return PolicyStructure(
        schema_version=schema_version,
        version=version,
        source_type=source_type,
        source_authoritative=source_authoritative,
        source_citation=source_citation,
        required_facts=tuple(fact_specs),
        clauses=tuple(clauses),
    )


def structure_to_json(structure: PolicyStructure) -> str:
    """Serialize a structure canonically (sorted keys) for storage and hashing."""
    return json.dumps(asdict(structure), sort_keys=True)


def structure_from_json(raw_json: str, context: str) -> PolicyStructure:
    """Rehydrate a stored structure, failing loudly on anything malformed."""
    if not raw_json or not raw_json.strip():
        raise ValueError(
            f"policy {context} has no structured content (structure_json is "
            "empty); it predates policy schema v2. Re-run 'medilens ingest'."
        )
    document = json.loads(raw_json)

    fact_specs: list[FactSpec] = []
    for raw_fact in document["required_facts"]:
        fact_specs.append(FactSpec(**raw_fact))

    clauses: list[ClauseSpec] = []
    for raw_clause in document["clauses"]:
        rule_spec = None
        if raw_clause["rule"] is not None:
            rule_spec = RuleSpec(**raw_clause["rule"])
        judgment_spec = None
        if raw_clause["judgment"] is not None:
            judgment_spec = JudgmentSpec(**raw_clause["judgment"])
        clauses.append(
            ClauseSpec(
                clause_id=raw_clause["clause_id"],
                title=raw_clause["title"],
                text=raw_clause["text"],
                evaluation=raw_clause["evaluation"],
                required=raw_clause["required"],
                bypasses=tuple(raw_clause["bypasses"]),
                rule=rule_spec,
                judgment=judgment_spec,
                source_ref=raw_clause["source_ref"],
            )
        )

    return PolicyStructure(
        schema_version=document["schema_version"],
        version=document["version"],
        source_type=document["source_type"],
        source_authoritative=document["source_authoritative"],
        source_citation=document["source_citation"],
        required_facts=tuple(fact_specs),
        clauses=tuple(clauses),
    )


def render_structure_text(service: str, structure: PolicyStructure) -> str:
    """Render a structured policy as a deterministic human-readable block.

    Shown to coders and included in the model's context. The clause_id appears
    on every clause so citations and judgments reference stable ids, never
    positions. Layout is deterministic so the content hash is stable.
    """
    lines: list[str] = []
    lines.append(f"Service: {service}")
    lines.append("")
    lines.append("Documentation criteria (referenced by clause_id):")
    for clause in structure.clauses:
        required_label = "required" if clause.required else "override"
        lines.append(
            f"[{clause.clause_id}] ({clause.evaluation}, {required_label}) "
            f"{clause.title}: {clause.text}"
        )
        if len(clause.bypasses) > 0:
            bypassed = ", ".join(clause.bypasses)
            lines.append(
                f"    When satisfied, makes not applicable: {bypassed}"
            )
    return "\n".join(lines)

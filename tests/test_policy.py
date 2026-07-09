"""Tests for the payer-policy layer: structure, parsing, hashing, ingestion,
date resolution, and service matching (policy schema v2).

Uses in-memory SQLite so the suite runs in CI without Docker. No real PHI is
involved, and the seed policies are synthetic (CLAUDE.md section 8).
"""

import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import medilens.policy as policy_package
from medilens.db.models import Base, PayerPolicy
from medilens.policy.ingest import (
    ParsedPolicy,
    compute_policy_hash,
    ingest_policies,
    parse_policy_seed_file,
)
from medilens.policy.retrieval import (
    find_policy_at_date,
    list_policies_for_payer_at_date,
    list_policies_for_service_at_date,
    service_matches,
)
from medilens.policy.structure import (
    parse_policy_structure,
    render_structure_text,
    structure_from_json,
    structure_to_json,
)

SEED_PATH = (
    Path(policy_package.__file__).parent / "seed" / "payer_policies_ortho_pain.yaml"
)

FIXED_RETRIEVED_AT = datetime.datetime(2026, 1, 15, 12, 0, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _raw_structure(clause_text: str = "First requirement.") -> dict:
    return {
        "schema_version": "policy-v2",
        "version": 1,
        "source": {
            "type": "synthetic",
            "authoritative": False,
            "citation": "test citation",
        },
        "required_facts": [
            {
                "key": "symptom_duration",
                "type": "duration",
                "unit": "weeks",
                "source": "note",
                "description": "duration",
            }
        ],
        "clauses": [
            {
                "clause_id": "duration",
                "title": "Duration",
                "text": clause_text,
                "evaluation": "hybrid",
                "required": True,
                "rule": {"op": "min_duration", "fact": "symptom_duration", "unit": "weeks", "minimum": 6},
                "judgment": {"question": "Documented?", "requires_evidence": True},
            }
        ],
    }


def _make_policy(
    payer_name: str = "Medicare",
    policy_identifier: str = "SYN-LUMBAR-MRI-001",
    clause_text: str = "First requirement.",
    effective_start: datetime.date = datetime.date(2025, 1, 1),
    effective_end: datetime.date | None = None,
    service: str = "Lumbar MRI (advanced imaging of the lumbar spine)",
    service_keywords: str = "lumbar mri,mri",
) -> ParsedPolicy:
    structure = parse_policy_structure(_raw_structure(clause_text), "test")
    structure_json = structure_to_json(structure)
    return ParsedPolicy(
        payer_name=payer_name,
        policy_identifier=policy_identifier,
        specialty="Orthopedics and pain medicine",
        service=service,
        service_keywords=service_keywords,
        policy_text=render_structure_text(service, structure),
        structure=structure,
        structure_json=structure_json,
        effective_start=effective_start,
        effective_end=effective_end,
        source="test source",
    )


# --- structure parsing and validation ----------------------------------------


def test_parse_structure_round_trips_through_json() -> None:
    structure = parse_policy_structure(_raw_structure(), "test")
    rehydrated = structure_from_json(structure_to_json(structure), "test")

    assert rehydrated == structure


def test_structure_from_empty_json_fails_loudly() -> None:
    with pytest.raises(ValueError, match="predates policy schema v2"):
        structure_from_json("", "SYN-OLD-001")


def test_parse_structure_rejects_duplicate_clause_ids() -> None:
    raw = _raw_structure()
    raw["clauses"].append(dict(raw["clauses"][0]))

    with pytest.raises(ValueError, match="duplicated"):
        parse_policy_structure(raw, "test")


def test_parse_structure_rejects_rule_with_undeclared_fact() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["rule"]["fact"] = "undeclared_fact"

    with pytest.raises(ValueError, match="undeclared_fact"):
        parse_policy_structure(raw, "test")


def test_parse_structure_rejects_unknown_operator() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["rule"]["op"] = "magic_op"

    with pytest.raises(ValueError, match="magic_op"):
        parse_policy_structure(raw, "test")


def test_parse_structure_requires_judgment_for_model_judged() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["evaluation"] = "model_judged"
    raw["clauses"][0]["rule"] = None
    raw["clauses"][0]["judgment"] = None

    with pytest.raises(ValueError, match="judgment"):
        parse_policy_structure(raw, "test")


def test_parse_structure_manual_review_carries_no_rule_or_judgment() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["evaluation"] = "manual_review"

    with pytest.raises(ValueError, match="manual_review"):
        parse_policy_structure(raw, "test")


def test_parse_structure_rejects_unknown_bypass_target() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["bypasses"] = ["nonexistent"]

    with pytest.raises(ValueError, match="nonexistent"):
        parse_policy_structure(raw, "test")


def test_parse_structure_rejects_self_bypass() -> None:
    raw = _raw_structure()
    raw["clauses"][0]["bypasses"] = ["duration"]

    with pytest.raises(ValueError, match="cannot bypass itself"):
        parse_policy_structure(raw, "test")


def test_parse_structure_rejects_authoritative_synthetic() -> None:
    raw = _raw_structure()
    raw["source"]["authoritative"] = True

    with pytest.raises(ValueError, match="authoritative"):
        parse_policy_structure(raw, "test")


def test_render_structure_text_shows_clause_ids_and_bypasses() -> None:
    raw = _raw_structure()
    raw["clauses"].append(
        {
            "clause_id": "red_flag",
            "title": "Red flag",
            "text": "Red flag documented.",
            "evaluation": "model_judged",
            "required": False,
            "bypasses": ["duration"],
            "judgment": {"question": "Red flag?", "requires_evidence": True},
        }
    )
    structure = parse_policy_structure(raw, "test")

    rendered = render_structure_text("Lumbar MRI", structure)

    assert "[duration]" in rendered
    assert "[red_flag]" in rendered
    assert "When satisfied, makes not applicable: duration" in rendered


# --- seed file ----------------------------------------------------------------


def test_parse_seed_file_loads_v2_policies() -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    identifiers = {policy.policy_identifier for policy in policies}
    assert identifiers == {
        "SYN-LUMBAR-MRI-001",
        "SYN-LUMBAR-ESI-001",
        "SYN-LUMBAR-RFA-001",
        "SYN-KNEE-INJ-001",
        "SYN-B-LUMBAR-MRI-001",
        "SYN-HIP-INJ-001",
    }
    for policy in policies:
        assert policy.structure.schema_version == "policy-v2"
        assert policy.structure.source_authoritative is False
        assert len(policy.structure.clauses) > 0
        assert policy.structure_json != ""


def test_seed_lumbar_mri_carried_by_two_payers_with_different_thresholds() -> None:
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}

    medicare = policies["SYN-LUMBAR-MRI-001"]
    payer_b = policies["SYN-B-LUMBAR-MRI-001"]
    assert medicare.payer_name == "Medicare"
    assert payer_b.payer_name == "National Commercial Payer B"

    medicare_duration = medicare.structure.clause_by_id("symptom_duration")
    payer_b_duration = payer_b.structure.clause_by_id("symptom_duration")
    # Same service, stricter commercial threshold: the divergence the eval
    # payer-B case exercises.
    assert medicare_duration.rule.params["minimum"] == 6
    assert payer_b_duration.rule.params["minimum"] == 12


def test_seed_knee_policy_uses_code_in_set() -> None:
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}
    knee = policies["SYN-KNEE-INJ-001"].structure

    covered = knee.clause_by_id("covered_indication")
    assert covered.evaluation == "deterministic"
    assert covered.rule.op == "code_in_set"
    assert "M17.12" in covered.rule.params["allowed"]


def test_seed_hip_policy_generalizes_code_in_set_to_second_joint() -> None:
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}
    hip = policies["SYN-HIP-INJ-001"].structure

    # code_in_set on hip codes, distinct from the knee policy's allowed set.
    covered = hip.clause_by_id("covered_indication")
    assert covered.evaluation == "deterministic"
    assert covered.rule.op == "code_in_set"
    assert set(covered.rule.params["allowed"]) == {
        "M16.11",
        "M16.12",
        "M25.551",
        "M25.552",
    }

    # The hip-specific criterion: a deep joint requires image guidance.
    guidance = hip.clause_by_id("image_guidance")
    assert guidance.evaluation == "model_judged"
    assert guidance.required is True
    assert guidance.judgment.requires_evidence is True


def test_seed_mri_policy_shape() -> None:
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}
    mri = policies["SYN-LUMBAR-MRI-001"].structure

    red_flag = mri.clause_by_id("red_flag")
    assert set(red_flag.bypasses) == {
        "symptom_duration",
        "conservative_therapy",
        "objective_findings",
        "not_recent_duplicate",
    }
    lookback = mri.clause_by_id("not_recent_duplicate")
    assert lookback.evaluation == "manual_review"
    duration = mri.clause_by_id("symptom_duration")
    assert duration.evaluation == "hybrid"
    assert duration.rule.op == "min_duration"


def test_seed_rfa_policy_has_history_frequency_limit() -> None:
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}
    rfa = policies["SYN-LUMBAR-RFA-001"].structure

    frequency = rfa.clause_by_id("frequency_limit")
    assert frequency.evaluation == "deterministic"
    specs = rfa.fact_specs_by_key()
    assert specs["rfa_same_level_12mo"].source == "history"
    assert specs["mbb_relief_percent"].source == "note"


# --- hashing -------------------------------------------------------------


def test_policy_hash_is_stable() -> None:
    policy = _make_policy()

    assert compute_policy_hash(policy) == compute_policy_hash(policy)


def test_policy_hash_changes_when_structure_changes() -> None:
    original = _make_policy(clause_text="Original clause.")
    revised = _make_policy(clause_text="Revised clause.")

    assert compute_policy_hash(original) != compute_policy_hash(revised)


def test_policy_hash_changes_when_effective_end_set() -> None:
    active = _make_policy(effective_end=None)
    retired = _make_policy(effective_end=datetime.date(2025, 12, 31))

    assert compute_policy_hash(active) != compute_policy_hash(retired)


# --- ingestion -----------------------------------------------------------


def test_ingest_writes_rows_with_structure(session: Session) -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    written = ingest_policies(session, policies, FIXED_RETRIEVED_AT)

    assert written == len(policies)
    stored = session.query(PayerPolicy).all()
    for row in stored:
        assert row.structure_json != ""
        # The stored structure rehydrates to a valid v2 structure.
        structure_from_json(row.structure_json, row.policy_identifier)


def test_ingest_is_idempotent(session: Session) -> None:
    policies = parse_policy_seed_file(SEED_PATH)

    first = ingest_policies(session, policies, FIXED_RETRIEVED_AT)
    second = ingest_policies(session, policies, FIXED_RETRIEVED_AT)

    assert first == len(policies)
    assert second == 0
    assert session.query(PayerPolicy).count() == len(policies)


def test_ingest_writes_new_version_when_structure_changes(session: Session) -> None:
    original = _make_policy(clause_text="Original clause.")
    ingest_policies(session, [original], FIXED_RETRIEVED_AT)

    revised = _make_policy(clause_text="Revised clause for 2026.")
    written = ingest_policies(session, [revised], FIXED_RETRIEVED_AT)

    # A changed policy is a new version; both coexist for audit history.
    assert written == 1
    assert session.query(PayerPolicy).count() == 2


# --- date-resolved retrieval ---------------------------------------------


def test_find_policy_in_force(session: Session) -> None:
    ingest_policies(session, [_make_policy()], FIXED_RETRIEVED_AT)

    found = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2026, 6, 1)
    )

    assert found is not None
    assert found.policy_identifier == "SYN-LUMBAR-MRI-001"


def test_find_policy_before_effective_start_returns_none(session: Session) -> None:
    ingest_policies(
        session,
        [_make_policy(effective_start=datetime.date(2025, 1, 1))],
        FIXED_RETRIEVED_AT,
    )

    found = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2024, 12, 31)
    )

    assert found is None


def test_find_policy_effective_end_boundary_is_inclusive(session: Session) -> None:
    ingest_policies(
        session,
        [_make_policy(effective_end=datetime.date(2025, 12, 31))],
        FIXED_RETRIEVED_AT,
    )

    on_last_day = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2025, 12, 31)
    )
    day_after = find_policy_at_date(
        session, "Medicare", "SYN-LUMBAR-MRI-001", datetime.date(2026, 1, 1)
    )

    assert on_last_day is not None
    assert day_after is None


def test_list_policies_filters_by_payer_and_specialty(session: Session) -> None:
    medicare = _make_policy(payer_name="Medicare", policy_identifier="SYN-A")
    commercial = _make_policy(
        payer_name="National Commercial Payer A", policy_identifier="SYN-B"
    )
    ingest_policies(session, [medicare, commercial], FIXED_RETRIEVED_AT)

    medicare_policies = list_policies_for_payer_at_date(
        session,
        "Medicare",
        "Orthopedics and pain medicine",
        datetime.date(2026, 6, 1),
    )

    returned_ids = {policy.policy_identifier for policy in medicare_policies}
    assert "SYN-A" in returned_ids
    assert "SYN-B" not in returned_ids


# --- service matching --------------------------------------------------------


def test_service_matches_simple_keyword() -> None:
    assert service_matches("lumbar MRI", "lumbar mri,mri")
    assert service_matches("MRI of the lumbar spine", "lumbar mri,mri")


def test_service_matches_multiword_keyword_requires_all_tokens() -> None:
    keywords = "epidural steroid injection,esi,epidural"
    assert service_matches("lumbar epidural steroid injection", keywords)
    # A more specific request than the policy label still matches via a keyword.
    assert service_matches(
        "left L4-L5 transforaminal epidural steroid injection", keywords
    )


def test_service_does_not_match_unrelated_request() -> None:
    assert not service_matches("lumbar MRI", "epidural steroid injection,esi,epidural")
    assert not service_matches("knee arthroscopy", "lumbar mri,mri")


def test_service_never_matches_empty_keywords_or_request() -> None:
    # Legacy rows (pre-service-matching) have empty keywords: never matched.
    assert not service_matches("lumbar MRI", "")
    assert not service_matches("", "mri")


def test_seed_injection_policies_disambiguate_by_joint() -> None:
    # Regression: the knee and hip injection policies must never both match one
    # request. A joint-less keyword (for example a bare "major joint injection")
    # is a token-subset of both a knee and a hip request and cross-matches, so a
    # knee note would be judged against the hip policy's covered-diagnosis set
    # (and vice versa) and fail. Every injection keyword must carry its joint.
    policies = {p.policy_identifier: p for p in parse_policy_seed_file(SEED_PATH)}
    knee_keywords = policies["SYN-KNEE-INJ-001"].service_keywords
    hip_keywords = policies["SYN-HIP-INJ-001"].service_keywords

    assert service_matches("major joint injection, knee", knee_keywords)
    assert not service_matches("major joint injection, knee", hip_keywords)
    assert service_matches("major joint injection, hip", hip_keywords)
    assert not service_matches("major joint injection, hip", knee_keywords)
    # A request that names no joint matches neither and is refused upstream.
    assert not service_matches("major joint injection", knee_keywords)
    assert not service_matches("major joint injection", hip_keywords)


def test_list_policies_for_service_filters_by_service(session: Session) -> None:
    mri = _make_policy(
        policy_identifier="SYN-A",
        service="Lumbar MRI",
        service_keywords="lumbar mri,mri",
    )
    esi = _make_policy(
        policy_identifier="SYN-B",
        service="Lumbar epidural steroid injection",
        service_keywords="epidural steroid injection,esi,epidural",
    )
    ingest_policies(session, [mri, esi], FIXED_RETRIEVED_AT)

    matching = list_policies_for_service_at_date(
        session,
        "Medicare",
        "Orthopedics and pain medicine",
        "lumbar epidural steroid injection",
        datetime.date(2026, 6, 1),
    )

    returned_ids = {policy.policy_identifier for policy in matching}
    assert returned_ids == {"SYN-B"}


def test_list_policies_excludes_out_of_force(session: Session) -> None:
    active = _make_policy(policy_identifier="SYN-ACTIVE", effective_end=None)
    retired = _make_policy(
        policy_identifier="SYN-RETIRED",
        clause_text="Old clause.",
        effective_end=datetime.date(2024, 12, 31),
    )
    ingest_policies(session, [active, retired], FIXED_RETRIEVED_AT)

    in_force = list_policies_for_payer_at_date(
        session,
        "Medicare",
        "Orthopedics and pain medicine",
        datetime.date(2026, 6, 1),
    )

    returned_ids = {policy.policy_identifier for policy in in_force}
    assert "SYN-ACTIVE" in returned_ids
    assert "SYN-RETIRED" not in returned_ids

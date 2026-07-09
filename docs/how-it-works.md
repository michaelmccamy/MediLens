# How MediLens works, front to back

A creator's guide to the whole system: the life of one request, the data
layers underneath it, the guarantees and where each one is enforced, and a
suggested reading order. Written against the codebase as of July 2026
(243 tests, policy schema v2).

The one-sentence mental model:

> Retrieval decides what the model is allowed to see, the model proposes
> evidence, code verifies every claim and computes every decision, and the
> audit store refuses anything unproven.

The model never decides coverage. It reads the note and proposes facts,
judgments, and codes, each with verbatim evidence. Everything that matters
(clause outcomes, the coverage determination, the denial-risk score) is
computed in plain Python from verified inputs. That is the architecture in
CLAUDE.md section 4 made concrete: a retrieval-augmented system, not a
freeform model recalling codes from memory.

---

## 1. Life of a request

Running example: the synthetic hip note
(`src/medilens/eval/notes/hip_injection_supported.txt`), requested service
"major joint injection, hip", payer Medicare, date of service 2026-06-01.

### Step 1: Entry (UI or CLI)

- `src/medilens/ui/app.py` renders the review form. The service and payer
  dropdowns are derived from the loaded policies (current versions only), so
  the form can never offer a service the system cannot assess. A
  shoulder-injection entry is appended deliberately to demonstrate refusal.
- `src/medilens/cli.py` offers the same pipeline as `medilens validate`.
- `src/medilens/notes/ingest.py` normalizes uploads (line endings, unicode
  punctuation) so span offsets are stable.

Both entries build a `ValidationRequest` and call `run_validation` in
`src/medilens/reasoning/pipeline.py`, the spine of the whole system.

### Step 2: PHI screen, before anything else

`assert_no_blocking_phi` (`src/medilens/phi/screening.py`) runs before
retrieval and before any model call. This deployment is not BAA covered, so a
note carrying high-confidence identifiers is refused outright (CLAUDE.md
guardrail 6). Nothing PHI-like may reach the API.

### Step 3: Retrieval, scoped and date-resolved

Still in `pipeline.py`:

1. Candidate codes: `list_codes_at_date`
   (`src/medilens/knowledge/retrieval.py`) returns the ICD-10-CM codes in
   force on the DATE OF SERVICE, not today (guardrail 5, implemented in
   `src/medilens/date_resolution.py`).
2. Payer policies: `list_policies_for_payer_at_date`
   (`src/medilens/policy/retrieval.py`) returns the payer's in-force,
   CURRENT (not superseded) policies for the specialty.
3. Service matching: `service_matches` keeps only policies whose curated
   keyword phrases match the requested service. Every token of at least one
   phrase must appear in the request, which is deterministic and auditable.
4. Refusal: if no policy governs the request, `NoApplicablePolicyError` is
   raised BEFORE any model call, naming the services that are loaded.
   Feeding an inapplicable policy to the model produces confused
   half-answers; refusing is the honest outcome.

For the hip example: 25 candidate codes, and exactly one policy
(SYN-HIP-INJ-001) survives service matching.

### Step 4: Prompt and model call

- `src/medilens/reasoning/prompts.py` loads a versioned template file
  (`src/medilens/prompts/validation_v3.txt`). The version string is recorded
  with every output; v1 and v2 are kept so old audit records stay
  reproducible.
- The policy structure is rendered into deterministic human-readable text
  (`render_structure_text` in `src/medilens/policy/structure.py`) and
  included with the note, the candidate codes, and the request metadata.
- `src/medilens/client/anthropic_client.py` makes the call with a JSON
  schema (`src/medilens/reasoning/schema.py`) so the model must return
  strict structured output. `rate_limiter.py` (client-side token bucket) and
  `retry.py` (exponential backoff with jitter on 429/529, respect
  retry-after, no retry on other 4xx) wrap every call.

### Step 5: What the model is asked to return (the v3 contract)

- `extracted_facts`: free clinical facts, each with a verbatim note span.
- `clinical_facts`: TYPED values the policy declared it needs (for example
  `hip_conservative_therapy_duration: value 8, unit weeks`), each with
  evidence. The model reports raw value plus unit; it never converts units.
  If the note does not document a fact, the model omits it.
- `clause_judgments`: for each judgment-bearing clause, a status plus
  evidence spans.
- `code_recommendations`: codes from the candidate set only, each with
  supporting spans and a rationale.
- `documentation_gaps`: always conditional ("If clinically accurate,
  document ...", guardrail 1).
- `coverage_rationale`: prose only, for display.

What the model may NOT emit: a determination, a denial score, or any claim
that a policy is met. Those are computed later, in code.

### Step 6: Verification (the trust boundary)

`verify_validation_output` (`src/medilens/reasoning/verification.py`)
mechanically re-checks every claim:

- Every cited span must locate verbatim in the note (with true offsets).
  A fabricated span is dropped, and whatever it supported falls with it.
- Typed facts are parsed by declared type; an unparseable value is dropped.
  Dropped means missing, and missing fails closed downstream.
- A clause judgment of `satisfied` without locatable evidence is DOWNGRADED
  to `insufficient_documentation`. The model cannot assert satisfaction
  into existence.
- Codes outside the candidate set, or without documentation support, are
  dropped.
- Judgments for clauses the policy never declared are ignored.

Every rejection is counted and logged (type only, never content).

### Step 7: Coverage evaluation (code decides)

`evaluate_policy_coverage` (`src/medilens/reasoning/coverage.py`) walks the
policy's clauses:

- `deterministic` clauses run their rule in `src/medilens/policy/rules.py`
  (operators: min/max_duration, min/max_count, frequency_limit, date_within,
  code_in_set, boolean_true/false). Code owns all unit conversion; an
  unconvertible unit fails closed.
- `model_judged` clauses take the VERIFIED judgment.
- `hybrid` clauses take the worse of rule and judgment.
- `manual_review` clauses always defer to a human (used for anything needing
  claims history, which no source provides in this deployment).
- Missing data resolves by the fact's declared source: a missing note fact
  is `insufficient_documentation` (silence never satisfies); a missing
  history fact is `manual_review`.
- Bypass overrides: a trigger clause (for example a red flag) that resolves
  satisfied with verified evidence makes its policy-declared `bypasses` list
  `not_applicable` (moot, never passed). Membership lives in the policy
  data; the engine never special-cases a clause name.

Then two pure functions compute the outcome from clause statuses alone:

- Determination, by precedence: `does_not_meet` beats `manual_review` beats
  `insufficient_documentation` beats `meets_criteria`.
- Denial-risk score, fixed bands: meets 0.15, does_not_meet 0.85,
  manual_review 0.50, insufficient 0.35 + 0.30 x (failing fraction).

For the hip example: all five clauses resolve satisfied (covered_indication
by the code_in_set rule against the verified M16.12 recommendation), so the
determination is meets_criteria and the score is 0.15.

### Step 8: Persistence (audit refuses the unproven)

`write_recommendation` (`src/medilens/audit/writer.py`) builds a
`RecommendationRecord` and `_reject_ungrounded` re-validates it AGAIN at the
boundary: unknown determinations, unknown clause statuses, codes without
cited spans, and gaps that are not conditional are all refused. Audit rows
(`Recommendation`, `AuditLogEntry` in `src/medilens/db/models.py`) are
append only and carry the model name, prompt template version, and
timestamp, so any recommendation is reconstructable (guardrail 7). No note
text or identifier is stored; the input is referenced by content hash.

### Step 9: Rendering

`src/medilens/ui/recommendation_view.py` maps the outcome to a display
model and `design.py` renders it: determination hero, clause-status card
with statuses and cited evidence, computed score, conditional gaps, and the
persistent honesty note (guardrail 8). Model-supplied text is HTML-escaped.

---

## 2. The data layers underneath

### Knowledge layer

`src/medilens/knowledge/`: a curated ICD-10-CM ortho/pain seed with
effective date ranges, hashed and idempotently ingested. HCPCS Level II and
NCCI edits are future layers. CPT is out of scope (no AMA license, CLAUDE.md
section 2).

### Policy layer (the product's core asset)

`src/medilens/policy/`: policies on the v2 mold (`docs/policy-schema.md`).
Each policy: payer, identifier, service plus service keywords, real-world
effective dates, source provenance (`authoritative: false` for everything
synthetic), and a structure of clauses as described above.

Versioning has two independent axes, and conflating them corrupts
date-of-service resolution:

- `effective_start` / `effective_end`: the payer's real-world policy window.
- `superseded_at`: OUR curation versions. Re-ingesting changed content
  stamps the prior row superseded (kept forever for audit, never consulted
  by retrieval). Exactly one current version exists per identifier.

Authoring is self-service: write YAML on the mold, dry-run it with
`medilens check-policies`, load it with `medilens ingest --policy-seed`.
The lint (`src/medilens/policy/lint.py`) rejects ambiguous keyword sets,
the bug class where one request matched two services. See
`docs/policy-authoring.md`.

### Model client

`src/medilens/client/`: official Anthropic SDK, model string in config
(`src/medilens/config.py`), API key from the environment, token-bucket rate
limiting, backoff retries, pre-send token counting, schema-enforced JSON.

---

## 3. The evaluation harness

`src/medilens/eval/` runs labeled synthetic cases
(`cases/ortho_pain_v1.yaml`) through the REAL pipeline and reports code
accuracy (precision/recall/F1), coverage determination accuracy, targeted
clause-status accuracy, denial precision/recall at a threshold (default
0.35) with a sweep, citation correctness, and refusal handling.
`manual_review` outcomes are excluded from denial metrics: they are
handoffs to a human, not predictions.

The honest caveat, printed in every report: the gold labels are SYNTHETIC
PLACEHOLDERS written by the developers. Green metrics prove the wiring
works end to end. They are not an accuracy claim until a certified coder
adjudicates the labels (`docs/eval-label-review-v1.md` is the review
packet; `src/medilens/eval/README.md` explains the fields).

---

## 4. The invariants and where they live

| Invariant | Enforced in | Proven by |
| --- | --- | --- |
| PHI never reaches the model | `phi/screening.py`, first line of the pipeline | `test_phi_screening.py`, pipeline tests |
| Codes and policies resolve at date of service | `date_resolution.py`, both retrieval modules | `test_knowledge.py`, `test_policy.py` |
| No policy for the service means refusal, before the model | `pipeline.py` (`NoApplicablePolicyError`) | `test_reasoning.py`, eval refusal case |
| Only current policy versions govern | `policy/retrieval.py` filter, supersession at ingest | `test_policy.py` supersession tests |
| Keyword sets must be unambiguous | `policy/lint.py`, enforced in `ingest_policies` | lint tests, `test_cli.py` |
| Silence never satisfies a criterion | `rules.py` missing-fact outcome, `coverage.py` | `test_coverage.py`, `test_rules.py` |
| Missing history defers to a human | fact `source: history` handling in `rules.py` | `test_coverage.py`, MRI/RFA eval cases |
| The model cannot assert satisfaction | evidence downgrade in `verification.py` | `test_reasoning.py` downgrade tests |
| Fabricated evidence dies at the boundary | span location in `verification.py` | fabrication regression tests |
| Determination and score are computed, never model-emitted | `coverage.py` pure functions | `test_coverage.py` |
| No upcoding | most-specific-supported prompt rules plus regression checks | upcoding regression tests |
| Audit records are reconstructable and grounded | `audit/writer.py` `_reject_ungrounded`, append-only rows | `test_audit.py` |
| Gaps are conditional, never instructions to add facts | prompt contract plus audit-boundary check | `test_audit.py`, `test_reasoning.py` |

---

## 5. Suggested reading path

Read in this order; each step assumes the previous ones.

1. `CLAUDE.md`: the constitution. Everything else implements it.
2. `docs/policy-schema.md`: the v2 policy design and its five decisions.
3. `src/medilens/db/models.py`: the four tables; short and heavily commented.
4. `src/medilens/policy/structure.py` then `rules.py`: the policy mold and
   the rule engine.
5. `src/medilens/reasoning/pipeline.py`: the spine; follow `run_validation`
   top to bottom.
6. `src/medilens/reasoning/verification.py`: the trust boundary.
7. `src/medilens/reasoning/coverage.py`: clause evaluation, determination
   precedence, score bands.
8. `src/medilens/audit/writer.py`: the last gate.
9. `src/medilens/client/`: rate limiting and retries.
10. `src/medilens/eval/runner.py`: how the metrics are computed.
11. `src/medilens/ui/app.py` and `recommendation_view.py`: presentation.

Then read the tests as executable documentation. `tests/test_coverage.py`
is the best single file for the fail-closed semantics;
`tests/test_reasoning.py` shows every way the model can misbehave and what
happens to each.

---

## 6. Glossary

- Clause statuses: `satisfied`, `not_satisfied`,
  `insufficient_documentation` (silence or unverifiable), `contradictory_documentation`
  (verified evidence both ways), `not_applicable` (mooted by a bypass),
  `manual_review` (needs a human or missing history). Only `satisfied` and
  `not_applicable` count toward meeting criteria.
- Determinations: `meets_criteria`, `insufficient_documentation`,
  `manual_review`, `does_not_meet`, by the precedence in section 1 step 7.
- Evaluation types: `deterministic`, `model_judged`, `hybrid`,
  `manual_review` (per clause, declared in the policy data).
- Fact sources: `note` (model extracts with evidence, missing fails closed),
  `history` (no source in this deployment, defers to a human), `request`
  (supplied with the request).
- Supersession: our curation versioning of policy rows, orthogonal to the
  payer's real-world effective dates.

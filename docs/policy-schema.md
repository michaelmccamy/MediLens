# Policy schema v2

Status: ACCEPTED 2026-07-08. All five open decisions in section 16 were
resolved as recommended: (1) denial score computed in code from clause
statuses, model supplies rationale prose only; (2) boolean OR-groups deferred,
overall determination is all-required-clauses with single-clause overrides;
(3) model extracts typed fact values with verified verbatim evidence, code
makes every threshold decision; (4) manual_review outcomes are excluded from
denial precision/recall and scored separately as needs-human-review; (5) the
section 5 starter operator vocabulary is the initial scope.

This defines the "mold" every payer policy conforms to before policy breadth
is added: the two existing policies are refactored onto it, plus one
structurally different exemplar (frequency-limited RFA).

AMENDMENTS 2026-07-09 (review corrections, reflected in the implementation):

1. Overrides are policy-level bypasses declared on the trigger clause, not
   per-clause pull lists. `not_applicable_if_satisfied` on the overridden
   clause is replaced by `bypasses: [clause_id, ...]` on the override clause
   (for example red_flag). When the trigger resolves satisfied with verified
   evidence, every listed clause becomes not_applicable: a red flag means
   "image now, skip the gating criteria," so it moots the ENTIRE declared
   prerequisite set, which may include a manual_review clause (rendering it
   moot is different from asserting it passed). Membership is explicit policy
   data; the engine special-cases no clause name, and the model can never
   trigger a bypass.
2. Imaging-recency lookbacks are manual_review, not model_judged. The MRI
   policy's not_recent_duplicate is the same claims-history question as RFA's
   frequency_limit and fails closed the same way: notes almost never
   affirmatively document the absence of prior imaging, so judging it from
   note text would punish clean notes. It defers to human review, with a
   declared source: history fact documenting the deterministic upgrade path.
3. Unit normalization is owned by code. For duration facts the model reports
   the value and unit exactly as the note documents them (for example value 2,
   unit months); code converts to the rule's threshold unit via an explicit
   conversion table (day/week/month aliases, months as a documented 30-day
   approximation) and fails closed (fact treated as undocumented) on any unit
   it cannot convert. The model never pre-normalizes.

## 1. Purpose and principles

The policy layer decides whether a note documents what a payer requires. Today
each policy is a block of free text with numbered clauses, and the model both
reads the note and asserts which clauses are met. That lets the model freestyle
policy satisfaction, and silence in the note can read as "met."

v2 removes that freedom. Three principles govern every decision below:

1. Fail closed. Absence of evidence is never satisfaction. Silence, unverifiable
   evidence, missing data, or an unavailable data source all resolve to
   insufficient documentation or manual review, never to satisfied.
2. Auditable. Every clause result records how it was decided (code or model),
   the exact rule or the cited note spans, and a status. The overall coverage
   determination is computed in code from clause statuses, never taken from the
   model.
3. The model proposes, code disposes. Anything deterministic is decided by a
   rule engine in code. The model may extract a value or make a qualitative
   judgment, but only with cited evidence that the verifier checks, and it never
   emits an overall "meets criteria" verdict.

Synthetic policies are not authoritative. This schema makes them well shaped and
fail closed; it does not make their criteria clinically authoritative. Every
policy carries source.authoritative = false until real, licensed policy text and
review exist.

## 2. Policy record fields

```yaml
schema_version: policy-v2          # the mold this record conforms to
policy_identifier: SYN-LUMBAR-MRI-001
version: 2                         # bumped on any clause change; old versions retained
payer_name: Medicare
specialty: Orthopedics and pain medicine
service: Lumbar MRI without contrast
service_keywords: [lumbar mri, mri, magnetic resonance imaging]
effective_start: 2025-01-01
effective_end: null                # null means open-ended
source:
  type: synthetic                  # synthetic | lcd | ncd | commercial_policy
  authoritative: false             # true only for real, reviewed policy text
  citation: "SYNTHETIC illustrative policy. Not authoritative payer text."
  retrieved_at: 2026-01-15
  url: null
required_facts: [ ... ]            # structured facts the rules consume (section 6)
clauses: [ ... ]                   # ordered clauses (section 3)
```

The whole record is versioned and content hashed exactly as today, so a changed
policy is a new version and both coexist for audit history.

## 3. Clause structure

```yaml
- clause_id: symptom_duration      # stable, unique within the policy, human-readable
  title: Symptom duration and character
  text: >                          # the human-readable clause, shown to the coder
    Radicular or low back symptoms documented for at least 6 weeks, unless a
    red-flag indication is present.
  evaluation: hybrid               # deterministic | model_judged | hybrid | manual_review
  required: true                   # counts toward the overall determination
  not_applicable_if_satisfied: [red_flag]   # override clause_ids (section 8)
  rule: { ... }                    # present for deterministic and hybrid (section 5)
  judgment:                        # present for model_judged and hybrid (section 9)
    question: "Are the documented symptoms low back or radicular in character?"
    requires_evidence: true
  source_ref: "SYNTHETIC clause. Illustrative."   # provenance for this clause
```

Clause IDs are stable strings, not positions, so a clause can be reworded or
reordered without breaking citations or audit records that reference it.

## 4. Evaluation types

| type            | who decides satisfaction | model role                        | can it be auto-satisfied? |
|-----------------|--------------------------|-----------------------------------|---------------------------|
| deterministic   | code (rule engine)       | none, or extract a fact + evidence | yes, if inputs available  |
| model_judged    | model                    | assert status with cited evidence  | yes, only with evidence   |
| hybrid          | code AND model           | extract fact + assert judgment     | yes, only if both pass    |
| manual_review   | nobody (defers to human) | none                               | never                     |

- deterministic: a rule over structured inputs (request metadata and/or a
  verified extracted fact) decides. The model never asserts the status; at most
  it supplies a fact value with a cited span.
- model_judged: no code rule exists (the criterion is genuinely qualitative).
  The model asserts a status and must cite evidence; the verifier checks the
  evidence is real but cannot recompute the decision.
- hybrid: both a deterministic rule and a model judgment must pass. Example:
  "completed at least 6 weeks of conservative therapy" splits into a
  deterministic duration check and a model judgment that the therapy was
  genuinely conservative care.
- manual_review: the tool asserts nothing and always yields manual_review. Used
  for criteria requiring records the tool does not have.

## 5. Deterministic rule operators

A rule reads from three namespaces:

- `request.*` : always available (date_of_service, payer_name, requested_service)
- `recommendation.code` : the diagnosis code under consideration
- `facts.<key>` : a model-extracted structured fact (section 6)

Starter operator vocabulary (extensible; each operator declares what a missing
input yields per section 8):

| operator          | reads                         | params                    | satisfied when                                   |
|-------------------|-------------------------------|---------------------------|--------------------------------------------------|
| min_duration      | facts.<key> (duration)        | minimum, unit             | value >= minimum (unit-normalized)               |
| max_duration      | facts.<key> (duration)        | maximum, unit             | value <= maximum                                 |
| min_count         | facts.<key> (count)           | minimum                   | value >= minimum                                 |
| max_count         | facts.<key> (count)           | maximum                   | value <= maximum                                 |
| frequency_limit   | facts.<key> (count, history)  | maximum, window_months    | count within window <= maximum                   |
| date_within       | facts.<key> (date)            | of, min_days, max_days    | fact date is min..max days from the `of` date    |
| code_in_set       | recommendation.code           | allowed[]                 | code is in allowed set                           |
| boolean_true      | facts.<key> (boolean)         | (none)                    | value is true                                    |
| boolean_false     | facts.<key> (boolean)         | (none)                    | value is false                                   |

Preconditions (evaluated before clauses; if a precondition fails the policy does
not apply): `effective_window` (date_of_service inside effective_start..end),
and `code_in_set` when the policy only covers specific diagnoses.

Rules are pure functions of verified inputs. They contain no note text and make
no network calls, so a clause decision is reproducible from the audit record.

## 6. Clinical facts (the model extraction contract)

Deterministic and hybrid rules consume structured facts the model extracts. Each
declared fact says where its value comes from, which sets the missing-data
behavior (section 8):

```yaml
required_facts:
  - key: symptom_duration
    type: duration            # duration | count | date | boolean
    unit: weeks
    source: note              # note | request | history
  - key: prior_mri_same_region_12mo
    type: count
    source: history           # external claims/imaging history, not in this deployment
```

The model returns a `clinical_facts` object. Each fact is a value plus a verbatim
evidence span (for `source: note` facts):

```yaml
clinical_facts:
  symptom_duration:
    value: 8
    unit: weeks
    evidence: "Low back pain radiating to left leg, 8 weeks duration"
```

Hard rule: if the model cannot find a value, it OMITS the fact. It must never
guess or infer a value to fill the field. A missing fact is the fail-closed
trigger, not an error to paper over.

## 7. Statuses

Clause status (one of):

- satisfied: criterion met, with verified support.
- not_satisfied: verified evidence the criterion is affirmatively NOT met (for
  example duration extracted as 3 weeks when the minimum is 6).
- insufficient_documentation: the note lacks enough verified information to
  decide. Silence lands here. Fail closed.
- contradictory_documentation: verified evidence points both ways (for example
  "no prior injection" and "third injection this year"). Fail closed, flag human.
- not_applicable: an override fired or a precondition makes the clause moot.
- manual_review: requires assessment the tool will not attempt (manual_review
  clause, or a rule whose data source is unavailable). Fail closed.

Only satisfied and not_applicable count as "clause met." Everything else fails
closed and does not count toward coverage.

Overall determination (computed in code from required clauses, by precedence):

1. any required clause not_satisfied or contradictory -> does_not_meet
2. else any required clause manual_review -> manual_review
3. else any required clause insufficient_documentation -> insufficient_documentation
4. else -> meets_criteria

The model never emits the overall determination.

## 8. Missing-data and fail-closed behavior

The core of the design. Per input:

- `source: note` fact missing, or its evidence fails verification -> the fact is
  treated as absent -> every rule reading it yields insufficient_documentation.
- `source: history` fact, when the history source is unavailable in this
  deployment (currently always) -> the rule yields manual_review. This is why a
  frequency limit cannot be auto-satisfied today: we have no claims history, so
  it defers to a human rather than pretending it passed.
- `source: request` fact is always present.

Hybrid combination (rule status R, judgment status J), after overrides:

- if an override fired -> not_applicable
- else if R or J is not_satisfied or contradictory -> that failing status
- else if R or J is insufficient_documentation or manual_review -> that status
- else (both satisfied) -> satisfied

Overrides: a clause listed as `not_applicable_if_satisfied: [X]` becomes
not_applicable only when clause X itself resolves to satisfied with verified
evidence. The model cannot mark a clause not_applicable directly.

## 9. Evidence requirements for model judgments

For each model_judged clause (and the judgment half of a hybrid), the model
returns a status and evidence:

```yaml
clause_judgments:
  objective_findings:
    status: satisfied
    evidence:
      - "Positive straight leg raise on the left at 40 degrees"
      - "Diminished sensation in the left L5 dermatome"
```

Requirements:

- status = satisfied requires at least one verbatim supporting span. No span ->
  the verifier downgrades to insufficient_documentation.
- status = not_satisfied or contradictory requires at least one span showing the
  conflicting or negating evidence.
- status = insufficient_documentation needs no span (that is its meaning).
- Every span must be a verbatim substring of the note (the existing
  whitespace-tolerant locator), or it is dropped and the status downgraded.

## 10. Verifier rules (enforcement)

After the model returns, before anything is shown or the overall determination is
computed, the verifier enforces:

1. No satisfied without evidence. A model_judged satisfied with zero verified
   spans downgrades to insufficient_documentation, with a recorded reason.
2. Facts verify or drop. A fact whose evidence span is not verbatim, or whose
   value is the wrong type or unit, is dropped and treated as absent.
3. Rules are authoritative for deterministic clauses. The model is not asked for
   a status on deterministic clauses; if it supplies one it is ignored.
4. Override integrity. not_applicable is only honored when the triggering clause
   is satisfied with verified evidence.
5. Overall is computed, never accepted. The overall determination and the denial
   risk are derived in code from clause statuses (section 11).
6. manual_review is sticky. A required manual_review clause cannot be overridden
   into a pass.

Every downgrade or drop is recorded in the same rejections list the pipeline
already surfaces to the coder and writes to the audit record.

## 11. Overall determination and denial risk

Consequence worth deciding explicitly (see open decisions): denial risk should
be derived in code from the overall determination and the set of failing
clauses, not emitted freely by the model. A computed score is auditable ("0.7
because clauses conservative_therapy and objective_findings are insufficient")
where a model number is not. The model would supply rationale prose only.

Proposed mapping (tunable, and tuned against the eval set):

- meets_criteria -> low band
- insufficient_documentation -> moderate band, scaled by how many required
  clauses are insufficient
- does_not_meet -> high band
- manual_review -> not scored as a denial prediction; surfaced as "needs human
  review" so it never inflates or deflates precision/recall

## 12. Worked example: SYN-LUMBAR-MRI-001 refactored

```yaml
schema_version: policy-v2
policy_identifier: SYN-LUMBAR-MRI-001
version: 2
payer_name: Medicare
specialty: Orthopedics and pain medicine
service: Lumbar MRI without contrast
service_keywords: [lumbar mri, mri, magnetic resonance imaging]
effective_start: 2025-01-01
effective_end: null
source:
  type: synthetic
  authoritative: false
  citation: "SYNTHETIC illustrative policy. Not authoritative payer text."
  retrieved_at: 2026-01-15
required_facts:
  - { key: symptom_duration, type: duration, unit: weeks, source: note }
  - { key: prior_mri_same_region_months, type: count, source: history }
clauses:
  - clause_id: symptom_duration
    title: Symptom duration and character
    text: >
      Radicular or low back symptoms documented for at least 6 weeks, unless a
      red-flag indication is present.
    evaluation: hybrid
    required: true
    not_applicable_if_satisfied: [red_flag]
    rule: { op: min_duration, fact: symptom_duration, unit: weeks, minimum: 6 }
    judgment:
      question: "Are the documented symptoms low back or radicular in character?"
      requires_evidence: true

  - clause_id: conservative_therapy
    title: Conservative therapy trial
    text: >
      A completed trial of conservative therapy (for example physical therapy
      and pharmacologic management) is documented, unless a red-flag indication
      is present.
    evaluation: model_judged
    required: true
    not_applicable_if_satisfied: [red_flag]
    judgment:
      question: "Is a completed conservative-therapy trial documented?"
      requires_evidence: true

  - clause_id: objective_findings
    title: Objective neurologic findings
    text: >
      Objective neurologic findings on examination (positive straight-leg-raise,
      dermatomal sensory loss, motor weakness, or reflex change) supporting
      radiculopathy are documented, or their absence is explained.
    evaluation: model_judged
    required: true
    judgment:
      question: "Are objective neurologic findings supporting radiculopathy documented, or their absence explained?"
      requires_evidence: true

  - clause_id: red_flag
    title: Red-flag override
    text: >
      A red-flag indication (cauda equina syndrome, progressive neurologic
      deficit, suspected malignancy or infection) is documented.
    evaluation: model_judged
    required: false            # an override, not itself required
    judgment:
      question: "Is a red-flag indication documented?"
      requires_evidence: true

  - clause_id: not_recent_duplicate
    title: No recent duplicate study
    text: >
      No MRI of the same region was performed recently for the same indication
      without a documented change in clinical status.
    evaluation: model_judged   # documentable in the note; upgrades to
    required: true             # machine_checkable once a claims-history source exists
    judgment:
      question: "Does the note state that no recent prior MRI of this region was performed for this indication, or explain an interval change?"
      requires_evidence: true
```

Notes on the example:
- symptom_duration is the hybrid demonstration: code checks 8 >= 6, the model
  judges the symptoms are radicular. Both must pass.
- not_recent_duplicate is model_judged today (the note can state it) and is a
  documented upgrade path to machine_checkable via a `source: history` count
  once claims data exists. That evolution changes one clause, not the policy.
- If red_flag resolves satisfied, symptom_duration and conservative_therapy
  become not_applicable in code.

## 13. Worked example: SYN-LUMBAR-RFA-001 (frequency-limited, fail-closed)

The structurally different exemplar. Radiofrequency ablation adds a
frequency-limit clause whose data source does not exist, so it correctly defers
to manual_review rather than fabricating a pass.

```yaml
schema_version: policy-v2
policy_identifier: SYN-LUMBAR-RFA-001
version: 1
payer_name: Medicare
specialty: Orthopedics and pain medicine
service: Radiofrequency ablation, lumbar facet
service_keywords: [radiofrequency ablation, rfa, facet ablation, medial branch]
effective_start: 2025-01-01
effective_end: null
source: { type: synthetic, authoritative: false, citation: "SYNTHETIC.", retrieved_at: 2026-01-15 }
required_facts:
  - { key: mbb_relief_percent, type: count, unit: percent, source: note }
  - { key: rfa_same_level_12mo, type: count, source: history }
clauses:
  - clause_id: diagnostic_block
    title: Positive diagnostic medial branch block
    text: >
      At least one diagnostic medial branch block with documented relief of at
      least 50 percent is recorded.
    evaluation: hybrid
    required: true
    rule: { op: min_count, fact: mbb_relief_percent, minimum: 50 }
    judgment:
      question: "Is a diagnostic medial branch block with documented relief recorded?"
      requires_evidence: true

  - clause_id: frequency_limit
    title: Frequency limit
    text: >
      No more than two radiofrequency ablation procedures at the same level in a
      rolling 12-month period.
    evaluation: deterministic
    required: true
    rule: { op: frequency_limit, fact: rfa_same_level_12mo, maximum: 2, window_months: 12 }
```

Because `rfa_same_level_12mo` has `source: history` and no history source exists,
frequency_limit resolves to manual_review, the overall becomes manual_review,
and the tool refuses to auto-approve. This is the fail-closed property made
concrete: the honest answer to "have they had too many already" is "a human must
check the claims history," not a guess.

## 14. Evaluation matrix: intentional per-clause failures

The eval set gains cases that fail on a specific clause, each labeled with the
clause it targets and the expected status, so a regression that lets a clause
silently pass is caught:

| case                          | note characteristic                                  | target clause          | expected clause status        | overall               |
|-------------------------------|------------------------------------------------------|------------------------|-------------------------------|-----------------------|
| duration too short            | symptoms documented for 3 weeks                      | symptom_duration       | not_satisfied                 | does_not_meet         |
| therapy silent                | no conservative therapy mentioned                    | conservative_therapy   | insufficient_documentation    | insufficient_docs     |
| findings absent               | no exam findings and no explanation                  | objective_findings     | insufficient_documentation    | insufficient_docs     |
| red-flag bypass               | cauda equina documented, no therapy                  | red_flag / therapy     | red_flag satisfied, therapy NA| meets_criteria        |
| contradictory frequency       | "no prior injections" and "fourth this year"         | frequency_limit-like   | contradictory_documentation   | does_not_meet         |
| rfa no history                | valid RFA request, block documented                  | frequency_limit        | manual_review                 | manual_review         |

These join the existing happy-path cases so the harness measures both correct
approvals and correct fail-closed behavior.

## 15. Migration and versioning

- This is a breaking schema change. Policies move from `criteria: [string]` to
  structured `clauses`. New records carry `schema_version: policy-v2`.
- The model output contract changes (it now returns `clinical_facts` and
  `clause_judgments` instead of citing clause numbers), so this needs prompt
  `validation_v3`. Prompts v1 and v2 stay on disk for audit reproducibility, as
  before.
- The v2 ingester parses and hashes the structured record. The retrieval and
  service-matching layers are unchanged.
- Rollout: refactor the two existing policies to v2, add SYN-LUMBAR-RFA-001,
  build the rule engine and the v2 verifier, add prompt v3, then the intentional
  failure eval cases. No new payers or services beyond this until the mold is
  proven.

## 16. Open decisions (need your call before we build)

1. Denial score: compute it in code from clause statuses (auditable), with the
   model supplying rationale prose only? Recommended yes. This is a real change
   from today's model-emitted number.
2. Clause combination: for now, overall = all required clauses satisfied or
   not_applicable, with single-clause red-flag overrides. Defer general boolean
   OR-groups (for example "meets 2 of 3") to a later version? Recommended defer.
3. Fact provenance: confirm the model-extracts-value / code-decides split is the
   intended shape. Values still originate from the model, but only as a typed
   value with a verified verbatim span; code makes every threshold decision.
4. manual_review in metrics: keep manual_review cases out of denial
   precision/recall (scored separately as "needs human review") so they do not
   distort the numbers? Recommended yes.
5. Operator scope: is the section 5 starter vocabulary enough to refactor the
   two policies plus the RFA exemplar, or do you want more operators defined now?
```

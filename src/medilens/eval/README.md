# Evaluation set: how to add and adjudicate cases

Audience: the certified coder (or clinician) reviewing and extending the
labeled evaluation set, plus whoever assists them. The current labels are
SYNTHETIC PLACEHOLDERS written by the developers; no metric is a real
accuracy claim until a certified coder has adjudicated the labels.

## What a case is

A case pairs one synthetic note with one request (service, date of service,
payer) and the gold answers. Cases live in `cases/ortho_pain_v1.yaml`; notes
live in `notes/`. Run the set with:

```
uv run medilens evaluate
uv run medilens evaluate --threshold 0.5
```

## Writing a note

- Synthetic only. Never a real patient, never real PHI, no real names, no
  phone/SSN/email (the PHI gate will refuse the note). Start the file with
  "SYNTHETIC NOTE. Not a real patient. Evaluation fixture only."
- Write it the way a real note reads. The most valuable notes are the messy
  ones: missing elements, near-threshold values, contradictions, red flags.

## Labeling a case

```yaml
- id: my-new-case
  note_file: my_note.txt
  requested_service: lumbar MRI without contrast    # plain language, no CPT
  date_of_service: 2026-06-01
  payer: Medicare
  expected_codes: ["M54.16"]        # codes a coder would accept; [] if none
  expected_denied: true             # null for refusals and manual_review
  expected_determination: does_not_meet
  expected_clause_statuses:         # only the clauses this case targets
    symptom_duration: not_satisfied
  label_rationale: >
    Why the labels are what they are, so the next reviewer can check your
    reasoning instead of re-deriving it.
```

Field meanings:

- expected_codes: the ICD-10-CM codes a certified coder would accept for this
  note. This is the label that most needs your judgment; two current cases
  (M54.50 vs M54.16 on the red-flag note, M54.59 vs none on the facet-pain
  note) are known adjudication questions.
- expected_determination: one of meets_criteria, insufficient_documentation,
  does_not_meet, manual_review. Computed by the system from clause statuses;
  your label says what it SHOULD compute.
- expected_clause_statuses: statuses for the specific clauses the case is
  designed to exercise (satisfied, not_satisfied, insufficient_documentation,
  contradictory_documentation, not_applicable, manual_review). You do not
  need to label every clause, only the targeted ones.
- expected_denied: whether the claim would be denied as documented. Set null
  when expect_refusal is true or when the expected determination is
  manual_review (those are excluded from denial precision/recall by design).

## Rules of thumb

- Silence fails closed. If the note does not document something, the right
  clause label is insufficient_documentation, never satisfied.
- History questions defer. Clauses needing claims or imaging history
  (frequency limits, recency lookbacks) are manual_review in this deployment.
- One case, one target. The best cases isolate a single clause or behavior so
  a regression points at exactly one thing.
- Disagree in writing. If you think a gold label is wrong, change it and say
  why in label_rationale; the git history preserves the discussion.

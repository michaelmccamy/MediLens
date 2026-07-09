# Authoring a payer policy (self-service guide)

How to add a new payer policy (and therefore a new service option in the
review UI) without touching application code. The dropdowns in the UI derive
from the loaded policies, so once your policy ingests, its service appears
automatically.

Full schema semantics live in `docs/policy-schema.md`. This guide is the
practical mold plus the workflow.

## Workflow

1. Copy the skeleton below into a new YAML file (or add a policy entry to
   `src/medilens/policy/seed/payer_policies_ortho_pain.yaml`).
2. Dry-run it. This parses the structure and lints for keyword ambiguity
   without touching the database:

   ```
   uv run medilens check-policies path/to/my_policy.yaml
   ```

3. Fix anything it reports, then load it:

   ```
   uv run medilens ingest --policy-seed path/to/my_policy.yaml
   ```

   (Plain `uv run medilens ingest` loads the bundled seed.) Ingest lints
   again against the policies already loaded, so a conflict with an existing
   policy is caught even if your file is clean on its own. Re-ingesting a
   changed policy supersedes the prior version automatically; nothing is ever
   deleted.

4. Restart or refresh the review app: the new service and payer appear in the
   dropdowns.

## Skeleton

```yaml
specialty: Orthopedics and pain medicine

policies:
  - payer_name: Medicare
    policy_identifier: SYN-EXAMPLE-001        # stable, unique per payer
    effective_start: 2025-01-01               # payer's real-world window
    effective_end: null                       # null = still in force
    service: "Cervical MRI (advanced imaging of the cervical spine)"
    # Keyword rules (the lint enforces these):
    # - every phrase must uniquely identify this service; include the body
    #   region or joint in EVERY phrase (a bare "injection" or "major joint
    #   injection" is ambiguous and will be rejected)
    # - the service label above must match these keywords (all tokens of at
    #   least one phrase appear in the label)
    service_keywords:
      - "cervical mri"
      - "mri cervical spine"
    source: "SYNTHETIC illustrative policy for development. Not authoritative."
    structure:
      schema_version: policy-v2
      version: 1
      source:
        type: synthetic          # synthetic | lcd | ncd | commercial_policy
        authoritative: false     # true ONLY for real payer text you can cite
        citation: "SYNTHETIC illustrative policy. Not authoritative payer text."
      required_facts:
        - key: symptom_duration
          type: duration         # duration | count | date | boolean
          unit: weeks
          source: note           # note | request | history (see below)
          description: "How long the symptoms have been present, as documented."
      clauses:
        - clause_id: symptom_duration          # stable, unique in this policy
          title: "Symptom duration (6 weeks)"
          text: >
            Symptoms are documented with a duration of at least 6 weeks.
          evaluation: hybrid     # deterministic | model_judged | hybrid | manual_review
          required: true
          rule: { op: min_duration, fact: symptom_duration, unit: weeks, minimum: 6 }
          judgment:
            question: "Are the documented symptoms of the covered character?"
            requires_evidence: true
          source_ref: "SYNTHETIC clause. Illustrative."
```

## Field reference

### Evaluation types (per clause)

- `deterministic`: decided entirely by the rule engine from verified facts.
  Needs `rule`, no `judgment`.
- `model_judged`: decided by a verified model judgment with cited note
  evidence. Needs `judgment`, no `rule`.
- `hybrid`: both; the worse outcome wins (a rule failure beats a judgment
  pass and vice versa). Needs `rule` and `judgment`.
- `manual_review`: always defers to a human (use for anything needing data no
  source provides, like claims history). Needs neither.

### Rule operators

`min_duration`, `max_duration`, `min_count`, `max_count` (threshold checks on
a declared fact; code owns all unit conversion), `frequency_limit` (maximum
occurrences, optional `window_months`), `date_within` (optional `min_days` /
`max_days` from the date of service), `boolean_true` / `boolean_false`, and
`code_in_set` (satisfied when a verified recommended code is in `allowed`;
the one operator that takes no fact).

Every operator except `code_in_set` needs `fact: <key>` referencing a
declared entry in `required_facts`. The parser rejects a rule that references
an undeclared fact.

### Fact sources (drives fail-closed behavior)

- `note`: the model must extract it with cited evidence. Missing means the
  clause resolves `insufficient_documentation` (silence never satisfies).
- `history`: claims or imaging history. No history source exists in this
  deployment, so the clause resolves `manual_review`.
- `request`: supplied with the request itself (for example date of service).

### Optional clause fields

- `required: false`: the clause is informational; it never drives the
  determination.
- `bypasses: [clause_id, ...]` on a trigger clause (for example a red flag):
  when the trigger resolves satisfied with verified evidence, every listed
  clause becomes `not_applicable` (moot, not passed). List every clause the
  emergency actually moots; the engine never guesses membership.

## Hard rules (will be rejected or must never be done)

- Do not use em dashes anywhere in policy text.
- No CPT descriptors. Reference the service in plain language.
- `source.authoritative: true` only for real, citable payer text. Synthetic
  or paraphrased content must carry `authoritative: false` and say so in the
  citation; the UI and audit trail surface this.
- Clause text must describe what the documentation shows, never instruct
  anyone to add facts. Documentation guidance elsewhere is always conditional
  ("If clinically accurate, ...").
- The lint rejects: a service label that does not match its own keywords, a
  label that matches another policy's keywords (same payer), and duplicate
  payer+identifier entries in one file.

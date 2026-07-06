# CLAUDE.md

Project guide for the payer-aware clinical coding and documentation validation tool.
Read this fully before writing or changing code. The compliance rules in this file are
non-negotiable and override any request that conflicts with them.

## Formatting rule for this file and all generated docs

Do not use em dashes anywhere. Use commas, colons, parentheses, or separate sentences instead.

---

## 1. What this product is (and is not)

This is a pre-claim denial prevention and documentation sufficiency tool. Given a clinical
note, patient context, requested service, date of service, and payer, it recommends supported
diagnosis codes, flags documentation gaps, and predicts denial risk before submission.

It is NOT:

- A reimbursement maximizer. Never optimize for the highest paying code.
- An autonomous coder. A human coder or provider makes every final decision.
- A claim submission system. It never submits anything to a payer.
- A tool that guarantees payer approval.

The guiding principle in every prompt, ranking, and UI string is: recommend the most accurate
supported code, never the highest paying one.

---

## 2. Locked decisions

Current decisions for the MVP. Revisit only deliberately, and update dependent code when you do.

- STACK: Python 3.11+.
- MODEL: Claude Sonnet 5 for all extraction, validation, and reasoning for now. Claude Haiku is
  a later cost optimization for high-volume extraction subtasks, not used yet. Keep the model
  string in config so this is a one-line change.
- PHI: synthetic and de-identified data only. No real PHI in any dev, test, or CI environment.
  Because no real PHI is processed yet, no BAA is required to build, and dev can use the
  standard first-party Anthropic API. See PHI-gated work below.
- DEPLOYMENT PATH FOR REAL PHI: deferred. Before any real PHI enters the system, choose and
  implement a BAA-covered path (HIPAA-ready first-party API organization, or Bedrock or Vertex
  under a cloud BAA). Until then, do not send anything that could be PHI to the API.
- CPT SCOPE: out of the MVP. No AMA license. Do not embed, store, or display CPT code
  descriptors. Reason over ICD-10-CM, HCPCS Level II, NCCI edits, and payer policy only. A
  requested procedure may be referenced by the plain-language service the provider asked for,
  not by CPT descriptor text.
- BEACHHEAD: orthopedics and pain medicine, focused on advanced imaging and injection
  procedures (for example lumbar MRI, epidural steroid injections, major joint injections,
  radiofrequency ablation). Payer list gets pinned to the first design partner. Default working
  set until then: major national commercial payers plus Medicare. (TODO: confirm final payer
  list when a design partner is signed.)

### PHI-gated work

The following require the BAA-covered deployment path to be in place first. Do not implement
them against real data until then:

- Any code path that sends real patient data to a model endpoint.
- Any storage of real PHI.
- Any batching of real PHI. Note the Anthropic first-party Batch API is not BAA covered, so it
  is fine for synthetic data now but off limits for real PHI later.

---

## 3. Compliance guardrails (hard rules, never violate)

1. No fabricated medical necessity. The tool may point out missing documentation elements,
   but it must never invent, assume, or imply clinical facts that are not in the record.
   Any documentation suggestion must be phrased as conditional on clinical accuracy, for
   example: "If clinically accurate, document symptom duration and prior conservative therapy."

2. No upcoding. Never recommend a higher paying code unless it is the most specific code the
   documentation actually supports. If two codes are supported, prefer specificity and
   accuracy, not payment.

3. Human in the loop. Every output is a recommendation for a person to review. Never present a
   recommendation as a final coding decision and never auto-apply one.

4. Grounding and provenance. Every recommendation must cite (a) the exact note span that
   supports it and (b) the specific rule or policy clause used. No freeform code guessing.
   If the note does not support a code, say so rather than inferring.

5. Date-of-service correctness. All code sets and payer policies must be resolved against the
   date of service, not today. Version this explicitly (see section 6).

6. PHI protection.
   - Never log PHI. Not in application logs, error traces, analytics, or model telemetry.
   - Redact or tokenize identifiers before they reach any place that is not BAA covered.
   - Only send PHI to a model endpoint that is covered by a signed BAA.
   - The Anthropic first-party Batch API, Files API, and Code Execution are NOT BAA covered.
     Do not route PHI through them. Use the synchronous Messages API under a HIPAA-ready
     organization, or a BAA-covered cloud path (Bedrock or Vertex).

7. Auditability. Every recommendation must be reconstructable: store inputs, retrieved
   sources, model name and version, prompt template version, and timestamp. Audit records are
   append only.

8. UI honesty. Include a persistent note in output surfaces along the lines of: "This
   suggestion is based only on documentation currently present in the note. Do not add
   documentation unless it is clinically accurate."

---

## 4. Architecture principles

Build this as a retrieval-augmented system, not a freeform model that recalls codes from
memory. Layers:

- Knowledge layer: ICD-10-CM, HCPCS Level II, NCCI edits, LCD and NCD data. All public.
  CPT is licensed and out of scope until licensed.
- Payer policy layer: curated, versioned medical policies and prior authorization criteria for
  the beachhead specialty and payer list. This is the highest value and hardest layer. Start
  narrow and manual.
- Reasoning layer: extract clinical facts from the note, match facts to code and coverage
  requirements, identify gaps, and explain each recommendation with citations.

Retrieval feeds the model the relevant, date-correct rules for each request. The model does
not decide codes from its own training memory.

Cost note: use prompt caching (which is BAA covered) for the large static policy context so it
is not re-sent on every call.

---

## 5. Model and API integration rules

### Provider client

- Use the official Anthropic Python SDK (`anthropic`). Do not hand-roll HTTP unless required by
  a Bedrock or Vertex path, in which case use the vendor SDK.
- Read the API key from an environment variable. Never hardcode secrets. Never commit `.env`.
- Set an explicit model string in config, not inline, so model swaps are one change.

### Rate limiting and retries

Anthropic rate limits are per tier and cover requests per minute and input and output tokens
per minute. Exact numbers change by tier, so read them from the current docs at
https://docs.claude.com/en/api/rate-limits rather than hardcoding assumptions.

Required client behavior:

- Respect the `retry-after` header on HTTP 429 responses. Wait at least that long.
- Use exponential backoff with jitter for 429 and 529 (overloaded) responses. Suggested:
  base delay 1s, multiplier 2, max delay 60s, max 5 retries, plus random jitter up to 1s.
- Do not retry 4xx errors other than 429 (they will not succeed on retry). Fail fast and log
  the error type (never the PHI payload).
- Implement a client-side token-bucket limiter so bursts do not exceed your tier. Track both
  request count and estimated token count per minute.
- Use the Token Counting API to estimate input tokens before sending, so you can throttle
  before hitting a hard limit.
- For high-volume extraction, prefer many small concurrent requests with a bounded worker pool
  over one giant request. Bound concurrency to stay under the requests-per-minute limit.

### Prompting

- Keep prompt templates in versioned files, not inline strings. Log which template version
  produced each output.
- Ask the model to return structured output (strict JSON, no prose, no markdown fences) for any
  result that maps to UI or storage. Parse defensively and handle malformed output.
- Instruct the model to cite note spans and policy clauses in the structured output.
- Set a low temperature for coding and validation work so results are stable and auditable.
- Never include real PHI in prompt examples committed to the repo.

### Determinism and reproducibility

- Persist the exact request (with PHI redacted or stored only in the BAA-covered store), the
  model and version, and the response, so any recommendation can be reconstructed for audit.

---

## 6. Data model requirements

- Every code set and policy record carries an effective date range. Queries resolve against the
  date of service.
- Payer policies are versioned. Store the source, retrieval date, effective dates, and a hash
  so you can detect changes and re-ingest.
- Recommendation records store: input reference, extracted facts, recommended codes, cited note
  spans, cited policy clauses, denial-risk score, model and prompt versions, and timestamp.
- Separate PHI storage from non-PHI operational data. PHI lives only in BAA-covered,
  encrypted-at-rest storage with access controls and audit logging.

---

## 7. Coding conventions

Match the existing codebase first. When existing patterns and this guide disagree, follow the
existing code and raise the conflict.

- Prefer explicit loop constructs over compressed expressions. Do not use list, dict, or set
  comprehensions where a plain `for` loop is clearer. Readability and step-by-step logic beat
  compactness in this codebase.
- Match existing variable naming and structure. Do not rename or restyle surrounding code while
  making a change.
- Write functions that do one thing. Keep coding, retrieval, and model-call logic separated.
- Type hints on public functions. Docstrings that explain why, not just what.
- No clever one-liners in place of clear multi-line logic.
- Fail loudly on unexpected model output or missing policy data. Do not silently guess.

When correcting or reviewing code, explain why an approach is fundamentally flawed, not just
what to change.

---

## 8. Testing and evaluation

- Unit tests for extraction, retrieval, date resolution, code compatibility checks, and the
  client retry and rate-limit logic.
- Use synthetic notes as fixtures. No real PHI in tests or CI.
- Maintain a labeled evaluation set (built with certified coder review) and track: code
  recommendation accuracy, denial-prediction precision and recall, and citation correctness.
- Decide and document the target tradeoff between false negatives (missed denial risk) and
  false positives (alert fatigue), and tune thresholds against it.
- Add a regression check that fails if any output recommends a higher paying code without
  stronger documentation support, or fabricates a fact not present in the note fixture.

---

## 9. Things NOT to build (for now)

- No universal autonomous coder across all specialties and payers.
- No automatic claim or authorization submission.
- No CPT descriptor display until AMA licensing is confirmed.
- No dependence on every payer providing clean API access.
- No feature that replaces certified coders.
- No batching of PHI through non-BAA-covered endpoints.

---

## 10. Definition of done for any change

- Compliance guardrails in section 3 are upheld.
- No PHI in logs, tests, fixtures, prompts, or analytics.
- New model calls have rate-limit handling, retries with backoff, and structured-output parsing.
- New recommendations carry citations and are written to the audit store.
- Tests pass, including the upcoding and fabrication regression checks.
- Any date-sensitive logic resolves against date of service, not the current date.

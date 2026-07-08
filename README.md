# MediLens

Payer-aware clinical coding and documentation validation tool. See [CLAUDE.md](CLAUDE.md) for
the full product scope, compliance guardrails, and architecture. Read it before changing code.

Synthetic and de-identified data only. See CLAUDE.md section 2 before sending anything to a
model endpoint or storing anything that could be PHI.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) if not already installed.
2. Copy the environment template and fill in real values:

   ```
   cp .env.example .env
   ```

3. Start local Postgres:

   ```
   docker compose up -d
   ```

   If this machine already runs Postgres on port 5432, set `MEDILENS_DB_HOST_PORT` to a free
   port (for example 5433) in `.env` and update the port in `DATABASE_URL` to match.

4. Install dependencies:

   ```
   uv sync --extra dev
   ```

5. Run the test suite (uses in-memory SQLite; no Postgres needed):

   ```
   uv run pytest
   ```

6. Load the curated code-set and payer-policy seeds into the database:

   ```
   uv run medilens ingest
   ```

   This creates the tables if needed and is idempotent (re-running writes only changed records).

7. Run the validate command. It retrieves the codes and payer policies in force on the date of
   service, calls the model with a versioned prompt template, verifies every citation against
   the note (fabricated spans, codes outside the candidate set, and nonexistent policy clauses
   are all rejected), prints the recommendation, and writes it to the append-only audit store.
   Requires ANTHROPIC_API_KEY and an ingested database:

   ```
   uv run medilens validate tests/fixtures/synthetic_notes/lumbar_mri_example.txt --requested-service "lumbar MRI" --date-of-service 2026-06-01 --payer Medicare
   ```

8. Run the review UI. It renders the review-surface design (denial risk, recommended codes with
   click-to-locate note spans and cited policy clauses, documentation gaps, extracted facts, and
   provenance). With ANTHROPIC_API_KEY and an ingested database it runs the real pipeline on the
   submitted note and writes an audit record; without them it degrades to a clearly-labeled
   SAMPLE that does not analyze the note. Synthetic, de-identified notes only.

   ```
   uv sync --extra ui
   uv run streamlit run src/medilens/ui/app.py
   ```

9. Run the evaluation harness. It runs a labeled set of synthetic cases through the pipeline and
   prints the section-8 metrics (code recommendation accuracy, denial-prediction precision and
   recall, citation correctness) with a denial-threshold sweep for tuning. Requires
   ANTHROPIC_API_KEY and an ingested database (one model call per case):

   ```
   uv run medilens evaluate
   uv run medilens evaluate --threshold 0.4
   ```

   IMPORTANT: the shipped gold labels are synthetic placeholders, not certified-coder judgments.
   The metrics demonstrate the harness; they are not a real accuracy claim until the labels are
   reviewed by a certified coder. See `src/medilens/eval/cases/`.

## Layout

- `src/medilens/config.py`: environment-derived settings.
- `src/medilens/cli.py`: command-line entrypoint (`ingest` and `validate` subcommands).
- `src/medilens/ingestion.py`: orchestrates loading both seed files into the database.
- `src/medilens/audit/`: append-only writer for recommendation and audit-log records.
- `src/medilens/ui/`: the review surface. `recommendation_view.py` is the display contract that
  mirrors the audit-store shape; `design.py` is a pure, escaping HTML renderer of the handoff
  design (all model/note content is HTML-escaped; every compliance string is preserved);
  `app.py` is the thin Streamlit host that captures input, runs the pipeline (or a labeled
  sample), and hands the verified view to the renderer. Citation chips click to locate the
  supporting span in the note panel.
- `src/medilens/client/`: Anthropic API client wrapper: token-bucket rate limiting
  (requests/min and tokens/min), retries with exponential backoff and jitter on 429/529,
  retry-after support, pre-send token counting, and schema-enforced structured JSON output.
- `src/medilens/knowledge/`: code-set ingestion and date-resolved retrieval. Ships a curated
  ortho/pain ICD-10-CM seed (`seed/`), an ingester that hashes and idempotently loads it, and
  retrieval that resolves codes against the date of service. HCPCS Level II and NCCI edits later.
- `src/medilens/policy/`: payer medical-necessity policy ingestion and date-resolved retrieval.
  Ships a curated, synthetic ortho/pain policy seed (`seed/`) with numbered, citable criteria,
  an idempotent versioned ingester, and retrieval scoped by payer, specialty, and requested
  service at the date of service. Each policy carries curated service keywords; a request whose
  service no loaded policy governs is refused before any model call, with the loaded services
  named. The seed criteria are synthetic development data, not authoritative payer text.
- `src/medilens/reasoning/`: the reasoning pipeline. Loads a versioned prompt template
  (`src/medilens/prompts/`), feeds the model the note plus date-resolved candidate codes and
  payer policies, and mechanically verifies the output before anything is shown or stored:
  every cited span must appear verbatim in the note, every code must be in the retrieved
  candidate set, every cited clause must exist in the retrieved policy, and every code needs
  located documentation support. Violations fail loudly and nothing is persisted.
- `src/medilens/prompts/`: versioned prompt template files. The version is recorded in every
  audit record.
- `src/medilens/notes/`: note ingestion. Reads .txt/.md/.rtf, normalizes unicode punctuation,
  line endings, and whitespace at the boundary so grounding offsets are consistent. The UI
  accepts file uploads through this layer.
- `src/medilens/phi/`: PHI screening gate. Refuses notes carrying high-confidence identifiers
  (SSN, email, phone, IP) before any text reaches the model, since this deployment is not
  BAA covered. A screening safety-net, not a compliant de-identifier (free-text names are not
  reliably caught).
- `src/medilens/eval/`: the evaluation harness (CLAUDE.md section 8). A labeled set of synthetic
  cases (`cases/` + `notes/`), pure metric functions (`metrics.py`: code accuracy, denial
  precision/recall, citation correctness), and a runner (`runner.py`) that drives the pipeline
  per case, scores refusals as first-class outcomes, and sweeps the denial threshold. The gold
  labels are synthetic placeholders pending certified-coder review; no metric here is a real
  accuracy claim until then.
- `src/medilens/db/`: SQLAlchemy models and session setup for non-PHI operational data.
- `src/medilens/prompts/`: versioned prompt templates.
- `src/medilens/hashing.py`: shared content-hash primitive for change detection across ingesters.
- `src/medilens/date_resolution.py`: shared "in force on the date of service" query filter.
- `tests/fixtures/synthetic_notes/`: synthetic note fixtures for tests. Never real PHI.

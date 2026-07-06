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

7. Run the validate command (currently a scaffold; extraction, retrieval, and reasoning layers
   are not yet implemented):

   ```
   uv run medilens validate tests/fixtures/synthetic_notes/lumbar_mri_example.txt --requested-service "lumbar MRI" --date-of-service 2026-06-01 --payer Medicare
   ```

## Layout

- `src/medilens/config.py`: environment-derived settings.
- `src/medilens/cli.py`: command-line entrypoint (`ingest` and `validate` subcommands).
- `src/medilens/ingestion.py`: orchestrates loading both seed files into the database.
- `src/medilens/audit/`: append-only writer for recommendation and audit-log records.
- `src/medilens/client/`: Anthropic API client wrapper: token-bucket rate limiting
  (requests/min and tokens/min), retries with exponential backoff and jitter on 429/529,
  retry-after support, pre-send token counting, and schema-enforced structured JSON output.
- `src/medilens/knowledge/`: code-set ingestion and date-resolved retrieval. Ships a curated
  ortho/pain ICD-10-CM seed (`seed/`), an ingester that hashes and idempotently loads it, and
  retrieval that resolves codes against the date of service. HCPCS Level II and NCCI edits later.
- `src/medilens/policy/`: payer medical-necessity policy ingestion and date-resolved retrieval.
  Ships a curated, synthetic ortho/pain policy seed (`seed/`) with numbered, citable criteria,
  an idempotent versioned ingester, and retrieval scoped by payer and specialty at the date of
  service. The seed criteria are synthetic development data, not authoritative payer text.
- `src/medilens/reasoning/`: fact extraction, code matching, gap analysis. Not yet built.
- `src/medilens/db/`: SQLAlchemy models and session setup for non-PHI operational data.
- `src/medilens/prompts/`: versioned prompt templates.
- `src/medilens/hashing.py`: shared content-hash primitive for change detection across ingesters.
- `src/medilens/date_resolution.py`: shared "in force on the date of service" query filter.
- `tests/fixtures/synthetic_notes/`: synthetic note fixtures for tests. Never real PHI.

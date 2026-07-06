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

4. Install dependencies:

   ```
   uv sync --extra dev
   ```

5. Run the test suite:

   ```
   uv run pytest
   ```

6. Run the CLI (currently a scaffold; extraction, retrieval, and reasoning layers are not yet
   implemented):

   ```
   uv run medilens tests/fixtures/synthetic_notes/lumbar_mri_example.txt --requested-service "lumbar MRI" --date-of-service 2026-06-01 --payer Medicare
   ```

## Layout

- `src/medilens/config.py`: environment-derived settings.
- `src/medilens/cli.py`: command-line entrypoint.
- `src/medilens/client/`: Anthropic API client wrapper: token-bucket rate limiting
  (requests/min and tokens/min), retries with exponential backoff and jitter on 429/529,
  retry-after support, pre-send token counting, and schema-enforced structured JSON output.
- `src/medilens/knowledge/`: code-set ingestion and date-resolved retrieval. Ships a curated
  ortho/pain ICD-10-CM seed (`seed/`), an ingester that hashes and idempotently loads it, and
  retrieval that resolves codes against the date of service. HCPCS Level II and NCCI edits later.
- `src/medilens/policy/`: payer policy retrieval. Not yet built.
- `src/medilens/reasoning/`: fact extraction, code matching, gap analysis. Not yet built.
- `src/medilens/audit/`: append-only audit record writing. Not yet built.
- `src/medilens/db/`: SQLAlchemy models and session setup for non-PHI operational data.
- `src/medilens/prompts/`: versioned prompt templates.
- `tests/fixtures/synthetic_notes/`: synthetic note fixtures for tests. Never real PHI.

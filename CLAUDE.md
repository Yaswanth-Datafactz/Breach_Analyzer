# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Build sprint in progress (compressed schedule — full deployed system due Fri Aug 14, 2026). **Read [docs/plan.md](docs/plan.md) first** — it is the source of truth for architecture, the pipeline-vs-agent boundary, the Decisions Register (orchestration, models per tier, infrastructure, ER design, corpus design — each with rejected alternatives), the schema, and the day-by-day schedule with cut-lines. Do not re-derive decisions already recorded there; update it if a decision changes.

Run/build/test/lint:
- DB: `docker compose up -d` (Postgres 16 on port 5434, project `breach_analytics`)
- Backend (from `backend/`, venv at `backend/.venv`): `.venv/bin/alembic upgrade head` · `.venv/bin/uvicorn app.main:app --reload --port 8002` · `.venv/bin/pytest` · `.venv/bin/ruff check app tests`
- Frontend (from `frontend/`): `npm run dev` (port 5175; copy `.env.example` → `.env` first — the shared `client.ts` defaults to UC2's port 8000 and `VITE_API_BASE_URL` overrides it) · `npm run build` · `npm run lint`
- Corpus: `backend/.venv/bin/python -m corpusgen --mini --seed 42 --out data/corpus-mini --manifest data/manifest-mini.json` (drop `--mini` for the full corpus; `--validate` re-checks an existing corpus against its manifest)

## What this project is

Breach Analytics at Scale (DataFactZ AI Engineering Internship, Use Case 3 — capstone): ingest a synthetic breach corpus (500+ mixed-format documents), extract personal data elements with tiered routing (free deterministic detectors → DeepSeek → Claude escalation), resolve them to unique individuals, and produce a defensible exposure table — one row per person, per-category flags, every flag traceable to an exact passage. A deterministic pipeline does the bulk path; four budgeted, fully-traced agents (orchestrator, exception investigator, ER adjudicator, QA auditor) handle judgment. The corpus generator and its ground-truth manifest are scored deliverables and the accuracy answer key.

## Hard rules that always apply (Handbook §6.2 / §7 — scored, non-negotiable)

- **API**: resource-oriented REST under `/api/v1`, correct HTTP methods/status codes, Pydantic request/response models on every endpoint, accurate OpenAPI docs, at least API-key auth. Never return HTTP 200 with an error in the body.
- **Database**: real relational schema, normalized and indexed, managed with Alembic migrations. ERD lives in the design doc.
- **Code structure**: layered backend (routers → services → repositories → data access), typed Python, config via environment variables, structured logging, centralized error handling, unit tests on core business logic — not boilerplate.
- **Scalability**: stateless API processes, async I/O for all LLM/network calls, background jobs for long-running work, explicit caching reasoning.
- **Frontend**: componentized React with a shared layout shell (reused, byte-identical, from Use Cases 1/2 — see docs/plan.md's Frontend reuse decision), loading/error states on every async call, zero console errors in the demo build.
- **Brand** (Handbook §7): gradient `#F4AD0B → #FC7900 → #E3434A`; primary orange `#FC7900`; navy `#182127` chrome; Inter typeface; **Lucide icons only** (no Font Awesome/Material/emoji); rounded-xl cards (12px) / rounded-md buttons (6px) / rounded-full pills, cards lift on hover (`translateY(-5px)` + shadow); dark mode default. Voice: confident, plainspoken, enterprise, no exclamation marks, "your teams" not "users."
- **Defensibility** (this use case's own hard rule, brief §2): no exposure flag is ever asserted without at least one passage-anchored `flag_evidence` row resolving to a real document passage with char offsets. A flag that cannot show its source passage is worthless in front of a regulator.
- **Nothing silently dropped** (brief §4): every ingested document ends in a terminal `done` state or has a `quarantines` row with a reason. The reconciliation query in docs/plan.md's Verification section must return zero.
- **Data safety** (brief, no exceptions): every identity and document is synthetic (seeded Faker via `corpusgen/`). Never introduce real personal data — a single real identifier is an automatic integrity finding. Never log `pii_elements.value_raw` (structlog redaction is configured — keep it that way).
- **Secrets**: API keys (DeepSeek, Anthropic, OpenAI) go in environment variables or `.env` files — never commit a key; a committed key is an automatic deduction.

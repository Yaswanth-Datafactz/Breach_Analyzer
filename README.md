# Breach Analytics at Scale — Use Case 3

DataFactZ AI Engineering Internship capstone. Ingests a synthetic breach corpus (500+
mixed-format documents), extracts personal data elements with cost-tiered routing
(deterministic detectors → DeepSeek → OpenAI escalation), resolves them to unique individuals,
and produces a defensible exposure table — one row per person, per-category exposure flags,
every flag traceable to the exact source passage. A deterministic pipeline handles the bulk
path; four budgeted, fully-traced agents (orchestrator, exception investigator,
entity-resolution adjudicator, QA auditor) handle judgment, with human approval gates on
consequential actions.

## Stack

FastAPI · PostgreSQL 16 (SQLAlchemy 2 + Alembic) · React 19 + Vite + Tailwind v4 ·
PyMuPDF / Tesseract OCR / python-docx / openpyxl · DeepSeek-V3.2 (tier 1) ·
gpt-5.6-terra (tier 2, text+vision) · gpt-5.6-sol (agents) · hand-rolled agent loop on
OpenAI native tool use with an MCP facade for dev-time tooling.

## Quick start

```bash
# 1. Database (port 5434 — runs alongside UC1/UC2)
docker compose up -d

# 2. Backend (port 8002)
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys — never commit .env
alembic upgrade head
uvicorn app.main:app --reload --port 8002

# 3. Frontend (port 5175)
cd frontend
npm install
npm run dev

# 4. Generate the corpus + ground-truth manifest (reproducible)
python -m corpusgen --seed 42 --out data/corpus --manifest data/manifest.json
```

## Repository layout

- `backend/` — FastAPI service: pipeline, detectors, tiered extraction, entity resolution,
  agents, accuracy + cost measurement
- `frontend/` — DataFactZ-branded React app: dashboard, exposure table, person evidence
  drill-down, review queue, agent traces
- `corpusgen/` — synthetic corpus generator + ground-truth manifest (scored deliverable)
- `deliverables/` — shipped documents (design doc, diagrams, deck, exports)
- `docs/` — local planning (gitignored): plan.md is the source of truth

All identities and documents are synthetic (seeded Faker). No real personal data exists
anywhere in this repository.

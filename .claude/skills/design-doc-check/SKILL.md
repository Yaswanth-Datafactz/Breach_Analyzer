---
name: design-doc-check
description: Check the UC3 design doc draft against the brief-derived checklist before submission
---

# Design doc pre-submission check

Run this against the current draft of `deliverables/UC3_Design_Doc_Yaswanth.pdf` (or its source document) before it is submitted. Work through every item; report each as PASS / FAIL / NEEDS-ATTENTION with the specific location in the draft. The stack-justification section is graded hardest — start there. `docs/plan.md` §2 (Decisions Register), §3, §9, §10, and §16 are the source of truth for what the doc must contain.

## 1. Stack justification (graded hardest)

- [ ] **Each** of the three core decisions — orchestration, models per tier, infrastructure — presents **at least 2 seriously-considered rejected alternatives**, with concrete reasons (cost figures, capability gaps, scope arguments), not strawmen. Cross-check against plan §2: D1 (full Azure; fully local OSS LLMs; SaaS PII detectors), D2 (LangGraph; Claude Agent SDK; Celery/Temporal), D3 (rejected tier-1/tier-2/agent models each named with the reason).
- [ ] Rejected alternatives read as genuinely considered — each states what it would have bought and why it lost, not just why it is bad.
- [ ] Model prices quoted anywhere are verified against the provider's current pricing page — **no unverified price or cost claim survives** (plan D3's own rule).

## 2. Architecture & schema

- [ ] Architecture diagram present (pipeline stages + the four agents beside, not inside, the loop) and matches plan §3.
- [ ] **ERD present and matches `backend/app/db/models.py`** — diff table-by-table: every table in models.py appears; key columns, FKs, and the notable constraints (append-only `identity_links`, partial unique on live `extraction_jobs`, `UNIQUE(person_id, category)`) are shown. An ERD drawn from memory instead of the code is a FAIL.
- [ ] **Pipeline-vs-agent boundary section** exists and makes all three supporting arguments from plan §3: **cost** (agents touch exceptions, not documents; sublinear growth), **defensibility** (pipeline replayable; agent decisions individually traced/reversible), **testability** (deterministic surface unit-tested; agent surface evaluated). The boundary rule itself is stated as one defensible sentence.
- [ ] The anticipated hostile questions are answered somewhere ("why is the orchestrator an agent", "why may the adjudicator merge without approval").

## 3. Cost section

- [ ] Measured full-corpus cost stated, traceable to `cost_events` rows (no number that cannot be reproduced by a query).
- [ ] **100K and 1M extrapolations** present, with methodology (per-file-class mean × class mix) and the sublinearity caveat for agent cost.
- [ ] **Two-config cost/accuracy curve** (Config A economy vs Config B assurance) with both measured by `run_accuracy_eval.py`, and a consultant recommendation written from the numbers, not from taste.
- [ ] Waste-control savings quantified: sha256 attachment dedup, spreadsheet header-mapping (the 80-person sheet ≈ one tier-1 call), prompt caching, tier-0-is-free — each with its measured saving; Batch API named as the 100K+ lever.

## 4. Accuracy section

- [ ] Person-level **precision/recall** (and F1) vs the manifest, with the matching methodology stated.
- [ ] **Per-flag table** — per-category P/R across all flag categories, plus the trap scorecard.
- [ ] **`wrongly_merged` reported as its own headline metric** (the shared-name failure), not buried in an average.
- [ ] Error analysis present: every non-TP class (missed_extraction, ocr_failure, er_split, er_overmerge, trap_fp, wrong_category) with counts and at least one drilled-through example each.

## 5. Risks & scope

- [ ] Risk list present (plan §14's risks are the floor) with mitigations, and the 100x-load section (plan §12) states the first thing to break.
- [ ] Out-of-scope items recorded as named future work, not silently absent.

## 6. Language & polish

- [ ] **Zero typos** — run an actual spell-check pass over the full text (e.g. `aspell`/`codespell` on the source), don't eyeball it.
- [ ] DataFactZ voice throughout: confident, plainspoken, enterprise. **No exclamation marks anywhere.** Says **"your teams," never "users."**
- [ ] No hedge words presenting measured facts ("should", "probably" attached to a number is a FAIL — either it was measured or it is labeled an estimate with its basis).
- [ ] Every figure/table is referenced from the text and every claim in the text about a number matches the table it cites.

Report the full checklist with verdicts, then a short ordered fix list (most submission-blocking first). Do not edit the draft yourself unless the caller asks.

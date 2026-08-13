# AI Usage Log — Breach Analytics at Scale (UC3)

**Author:** Yaswanth Thottempudi | **Current through:** August 14, 2026

This capstone was built with extensive, hands-on use of **Claude Code** (Anthropic's CLI
coding agent, Claude Sonnet 5) as an implementation partner, directed and verified by me
throughout. This log is an honest account of that division of labor, not a minimized one — an
AI Engineering internship capstone should show real fluency working with these tools, not
distance from them.

## What I directed

- The compressed timeline decision (4 days instead of the brief's two-week cadence) and the
  explicit instruction to document every architectural decision in `docs/plan.md` before
  implementation began.
- The stack decisions at the point of genuine tradeoff: hybrid infrastructure (open-source
  components + API LLMs), a hand-rolled agent loop over a framework, Azure for deployment.
- The mid-build provider swap from Anthropic to OpenAI when no Anthropic credential was ever
  provisioned, and the follow-on discovery that the provisioned key was scoped to a specific
  Azure AI Foundry deployment rather than the public OpenAI API — I supplied the actual
  provisioning facts (the Foundry portal screenshot) that made the second, correct diagnosis
  possible.
- Two explicit, hardening budget instructions during the first live-keyed run ("make sure the
  budget does not exceed $30," revised to "hardwire it" at $20) — the resulting real,
  code-level cost ceiling (checked after every completed document, not just estimated) was
  built and tested in direct response to those instructions, not offered unprompted.
- An explicit instruction to hold all Azure deployment until told otherwise, honored throughout
  the live-run phase of the build.
- Continuous status checks and pacing decisions across every long-running phase (corpus
  generation, the live pipeline run, the agent demo traces, the accuracy evaluation).

## What Claude Code executed

- The full backend (FastAPI/SQLAlchemy/Alembic), frontend (React/Vite/Tailwind, reusing the
  shared shell from the program's earlier use cases), and the corpus generator, under the
  architectural decisions above.
- The tiered extraction adapters and the hand-rolled `AgentRunner`, including the full
  provider-neutral wire-format translation layer that let the Anthropic→OpenAI swap change zero
  lines of the runner's control flow — a design choice proposed and implemented by Claude Code,
  then validated live.
- The corpus generator's scenario-object edge-case planting (shared names, nickname clusters,
  partial identifiers, false-positive traps, problem files) and the accuracy harness that scores
  the system against the resulting manifest.
- Live debugging during the first real API-keyed run: diagnosing the Azure endpoint mismatch
  from a byte-level key comparison and a real 401 response (not a guess), and finding and fixing
  a genuine bug — the investigator's OCR tool crashing the whole agent run on a malformed PDF
  instead of degrading gracefully — discovered by the first live agent dispatch and fixed with a
  regression test in the same session.
- All four required agent demo traces, executed against real API calls, including recognizing
  live that the live model's efficiency meant a fixed 4-step "force failure" budget did not
  reliably produce a `budget_exceeded` outcome, and adjusting the constraint until it did — a
  real finding about model behavior, not a scripted result.
- This design document, problem statement, architecture diagrams, and presentation deck,
  drafted from the real measured numbers and the decision history recorded in `docs/plan.md`
  throughout the build — not written after the fact from memory.

## Verification discipline

Every number in this design document — accuracy, cost, per-category precision/recall, the trap
scorecard — comes from a real live-keyed run scored against the ground-truth manifest, not a
simulated or hand-estimated figure. Every architectural claim about provider pricing or API
behavior in `docs/plan.md`'s Decisions Register is either cited to a primary source or
explicitly flagged where the primary source could not be verified and a secondary source was
used instead (the pricing citation in Decision D10 is the clearest example: the official Azure
pricing page renders its table client-side and returned no numeric values through the available
fetch tooling, so the figure was cross-checked against two independent pricing trackers instead,
and that limitation is stated in the document rather than hidden).

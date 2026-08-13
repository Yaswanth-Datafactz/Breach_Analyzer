"""Environment-driven application settings (Handbook §6.2: config via env vars).

Shape copied from Document_Extraction/backend/app/core/config.py (UC2). New
settings groups UC2 didn't need: the OpenAI tier-2/agent models (docs/
plan.md D3), the ER band thresholds (D5), and the per-agent budget defaults
(docs/plan.md §3's agent table) -- all live here, versioned, so a price
change or a threshold recalibration is a config diff, not a doc rewrite.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database (host port 5434 -- 5432/5433 are already held by UC1's and
    # UC2's postgres containers when the projects run side by side on this
    # machine; docs/plan.md §7's port table)
    database_url: str = (
        "postgresql+psycopg://breach_analytics:breach_analytics_dev@localhost:5434/breach_analytics"
    )

    # API auth
    api_key: str = "changeme-dev-key"

    # CORS (comma-separated origins). Vite dev server (5175 -- docs/plan.md
    # §7's port table) plus the production preview build (4173, `npm run
    # preview`'s default) -- UC2's verification checklist checked the console
    # against the PRODUCTION build, which a dev-server-only origin silently
    # broke there; guarded here from the start.
    cors_origins: str = "http://localhost:5175,http://localhost:4173"

    # DeepSeek -- tier 1, cheap text-only extraction (docs/plan.md D3).
    # Hosted via Azure AI Foundry's Model Inference API, not api.deepseek.com
    # directly (UC2's proven wiring, reused verbatim) -- `deepseek_model` is
    # the Foundry DEPLOYMENT NAME, not DeepSeek's own model id, and is the
    # value the API actually expects in the request body. Auth is a plain
    # Bearer token (see UC2's core/config.py for the full Foundry addendum).
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://ai-training-msftfoundry.services.ai.azure.com/models"
    deepseek_api_version: str = "2024-05-01-preview"
    deepseek_model: str = "DeepSeek-V3.2"

    # OpenAI -- tier 2 escalation (text + vision) and the agent layer
    # (docs/plan.md D3, revised 2026-08-12: swapped in for Anthropic -- no
    # Anthropic key is available in this environment, only an OpenAI one.
    # REVISED AGAIN 2026-08-13: "an OpenAI one" turned out to mean an Azure
    # AI Foundry resource key, not a platform.openai.com key -- confirmed
    # live (a real call to api.openai.com with this key 401'd; a call to
    # this project's own Foundry resource at the classic Azure OpenAI
    # deployments path succeeded). This is the SAME Foundry resource
    # DeepSeek uses (`deepseek_base_url`'s host), exposed through a second,
    # different endpoint surface for native OpenAI deployments -- Azure AI
    # Foundry's unified Model Inference API (`.../models/chat/completions`,
    # deployment name in the request body) serves catalog/partner models
    # like DeepSeek; native OpenAI deployments instead sit behind the
    # classic Azure OpenAI deployments API
    # (`.../openai/deployments/{name}/chat/completions?api-version=...`,
    # deployment name IN THE URL). Both are Chat-Completions-shaped, so
    # OpenAIModelClient/OpenAIExtractionAdapter's translation logic is
    # unchanged -- only the SDK client class changes, `openai.OpenAI` ->
    # `openai.AzureOpenAI` (`AsyncAzureOpenAI` for the extraction adapter),
    # which needs `openai_base_url` + `openai_api_version` alongside the
    # key. Confirmed live: `Authorization: Bearer` AND the classic `api-key`
    # header both work; api-version 2025-04-01-preview works (older
    # 2024-10-21 and 2025-01-01-preview also worked for chat/completions,
    # 2025-04-01-preview picked for margin -- it is also the first version
    # this resource accepts for the Responses API, should a future need
    # arise). A live completion also confirmed `message.refusal` is a real
    # field, `null` on a normal completion -- the defensive
    # getattr(..., None) in model_client.py's _map_stop_reason was
    # therefore the right shape, now verified rather than assumed.
    #
    # `tier2_model`/`agent_model` BOTH point at "gpt-5.5", the one
    # deployment that actually exists on this Foundry resource (portal:
    # Global Standard, model version 2026-04-24) -- there is no second,
    # stronger deployment for the agent role the way the original
    # sol/terra split assumed. This is a pragmatic simplification, not an
    # oversight: both config knobs stay distinct settings (a second
    # deployment added later is a config change, not a code change), and
    # the required cost/accuracy comparison (docs/plan.md §9 Config
    # A/B) varies escalation threshold/vision/gray-band width, not the
    # tier-2 model identity, so a single verified model does not block it.
    # Prices verified 2026-08-13: the official Azure pricing page
    # (azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service)
    # renders its price table client-side (fetched as literal "$-"
    # placeholders through this environment's fetch path, not a real
    # number) -- cross-checked instead against two independent pricing
    # trackers (a Cloudzero-referencing search summary and benchlm.ai,
    # last synced 2026-07-25) that agree exactly: $5.00 / $30.00 /
    # $0.50 per 1M input/output/cached-input tokens for gpt-5.5 Global
    # Standard. Flagged as secondary-source-verified, not primary-source,
    # given the JS-rendering gap -- re-check against the Azure pricing
    # calculator directly if this number matters for a high-stakes claim.
    # gpt-5.5 is a reasoning model: no temperature/top_p/logprobs (see
    # MODEL_PRICES_USD_PER_MTOK's note and services/confidence.py) --
    # confirmed live (the model_client.py adapter never sends these and
    # every live call succeeded) as well as via OpenAI's reasoning-models
    # guide (developers.openai.com/api/docs/guides/reasoning).
    openai_api_key: str = ""
    openai_base_url: str = "https://ai-training-msftfoundry.cognitiveservices.azure.com"
    openai_api_version: str = "2025-04-01-preview"
    tier2_model: str = "gpt-5.5"
    agent_model: str = "gpt-5.5"

    # Tier escalation threshold theta (docs/plan.md §9): composite confidence
    # below this escalates a tier-1 extraction to tier 2. Config A "economy"
    # runs 0.6, Config B "assurance" runs 0.8 -- both measured Friday.
    tier2_escalation_threshold: float = 0.8

    # Tier-1 chunking (docs/plan.md §9: "~2-3K-token passage-aligned chunk";
    # §14b: docx is 1 passage/paragraph, so the chunker aggregates). Token
    # counts are estimated at ~4 chars/token -- an estimate is all routing
    # needs, and a tokenizer dependency would buy false precision.
    extraction_chunk_max_tokens: int = 3000

    # Spreadsheet LLM sampling (docs/plan.md §9: deterministic header-mapping
    # first; the LLM sees only unmapped/ambiguous columns + a sample of rows,
    # so the 80-person sheet costs ~ one tier-1 call, not 80).
    sheet_sample_rows: int = 5

    # Vision escalation (docs/plan.md §9: low OCR confidence -> tier-2
    # vision). §14b R1 measured the BulkSpreadsheet PNGs at ocr_mean_conf
    # 51-57 with near-zero recovery -- 60 routes those to vision while
    # healthy scans (tuned to 80-95% recovery) stay on the text path.
    vision_ocr_conf_threshold: float = 60.0

    # ER band thresholds (docs/plan.md D5): >= auto_link merges by rule,
    # <= distinct stays separate, the gray zone in between goes to the
    # adjudicator agent. Calibrated against the FULL data/manifest.json via
    # scripts/calibrate_er.py: (0.85, 0.40) matches (0.85, 0.45)'s F1=0.973
    # and zero shared-name leakage at every threshold tested, but queues 5
    # more true gray pairs (36 vs 31) for the adjudicator -- ~$1.50 extra
    # at the $0.30/pair budget for a higher achievable recall ceiling
    # (0.963 -> 0.965). Applied 2026-08-12; rerun after any corpus change.
    er_auto_link_threshold: float = 0.85
    er_distinct_threshold: float = 0.40

    # Agent budget defaults (docs/plan.md §3's table -- steps / tokens / USD
    # checked between turns by services/agents/runner.py; exceeding any parks
    # the run with partial findings).
    orchestrator_max_steps: int = 15
    orchestrator_max_usd: float = 1.00
    investigator_max_steps: int = 12
    investigator_max_tokens: int = 60_000
    investigator_max_usd: float = 0.50
    adjudicator_max_steps: int = 10
    adjudicator_max_usd: float = 0.30
    auditor_max_steps_per_flag: int = 3
    auditor_max_usd_per_run: float = 2.00

    # Content-addressed original storage + rasterized page-image cache
    # (UC2's sha256-keyed, gitignored, regenerable store -- docs/plan.md §11:
    # quarantined originals served only via authenticated /documents/{id}/file).
    storage_root: str = "./storage"

    # Parsing / routing thresholds (docs/plan.md §3: OCR when the image-based
    # heuristic fires -- <120 chars/page or garbage ratio >0.3). Provisional
    # until measured against the generated corpus's known scanned set.
    ocr_min_chars_per_page: int = 120
    ocr_min_char_density_ratio: float = 0.08
    ocr_max_garbage_ratio: float = 0.30
    render_dpi: int = 300

    # Review-queue thresholds (docs/plan.md §4 review_items). TODO-calibrate
    # Wed against the manifest -- provisional starting points, same
    # provisional -> measured lifecycle UC2's review thresholds went through.
    review_flag_confidence_threshold: float = 0.80
    review_er_confidence_threshold: float = 0.85

    # Background job concurrency (docs/plan.md §14: full-corpus run at 3-5)
    extraction_max_concurrency: int = 3

    # Logging
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


# Per-model price table, USD per 1M tokens. DeepSeek is keyed by Foundry
# DEPLOYMENT name (UC2's convention -- matches what config puts in
# `extraction_jobs.model` and `cost_events.model`); OpenAI models by their
# API model id.
#
# DeepSeek figures carried over from UC2's table (third-party-aggregator-
# sourced there, pending direct Azure-billing confirmation -- see UC2's
# core/config.py).
#
# Superseded claude-sonnet-4-6/claude-opus-5/claude-haiku-4-5 entries
# removed outright (docs/plan.md D3 swap to OpenAI, 2026-08-12) -- nothing
# in the running system will ever hit an Anthropic model id again, so
# keeping them commented out would be dead weight, not a useful history
# (docs/plan.md itself is the historical record of the swap).
#
# gpt-5.5 figures verified 2026-08-13 (config.py's OpenAI settings block
# has the full citation + the JS-rendering caveat on the primary source) --
# $5.00/$30.00/$0.50 input/output/cached-input per 1M tokens. Reasoning
# model: no logprobs, no temperature/top_p (services/confidence.py
# renormalizes around the missing logprob signal exactly as it did for
# Anthropic, for this new reason).
# Re-verify ALL of these again against the providers' own pricing pages
# before Friday's report if any more time elapses (docs/plan.md D3: never
# quote unverified) -- prices move fast on frontier models.
MODEL_PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "DeepSeek-V3.2": {"input": 0.58, "output": 1.68},
    # gpt-5.6-sol/terra/luna (three-tier split) removed 2026-08-13: this
    # Foundry resource has exactly one native-OpenAI deployment, "gpt-5.5"
    # (config.py's OpenAI settings block has the full story + price
    # citation) -- keeping priced entries for deployments that do not
    # exist here risked a silent misconfiguration if tier2_model/
    # agent_model were ever pointed at one by typo. Add a priced entry
    # here when a second deployment actually exists to route to.
    "gpt-5.5": {"input": 5.00, "output": 30.00, "cached_input": 0.50},
}


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""DeepSeek extraction adapter -- the cheap tier-1 text-only path
(docs/plan.md D3). Copied near-verbatim from Document_Extraction/backend/
app/services/extraction/deepseek.py (UC2). Hosted via Azure AI Foundry's
Model Inference API (`.../models/chat/completions?api-version=...`), not
api.deepseek.com directly -- see core/config.py. That endpoint is
OpenAI-chat-completions-compatible over plain HTTP, so this still uses
`httpx.AsyncClient` directly rather than a dedicated SDK, with Bearer auth
(this Foundry resource's own generated quickstart uses the plain OpenAI
SDK's `api_key` param, which sends `Authorization: Bearer`, not Azure
OpenAI's classic `api-key` header -- confirmed from the portal by UC2, not
assumed). `response_format: json_object` guarantees valid JSON but NOT
schema conformance, so every response still goes through Pydantic
validation + the one bounded repair -- this adapter never assumes its own
output is well-typed. `model` in the request body is the Foundry DEPLOYMENT
name, never DeepSeek's own model id.

An explicit request timeout is set from the start -- UC1's DeepSeek adapter
shipped without one and httpx's 5s default caused intermittent ReadTimeout
in production as prompts grew (see UC2's copy of this module for the full
post-mortem). Guarded here before that bug has a chance to recur a third
time.
"""

from __future__ import annotations

import json

import httpx

from app.services.extraction.base import ExtractionAdapter, ExtractionResult, ExtractionUsage

_REQUEST_TIMEOUT_SECONDS = 90.0


class DeepSeekExtractionAdapter(ExtractionAdapter):
    provider_id = "deepseek"
    supports_vision = False

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        api_version: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_version = api_version
        # Tests inject a fake client via httpx.MockTransport -- request/
        # response shape is verified without ever making a live call.
        self._client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)

    async def extract_text(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(system_prompt, user_prompt)
        return self._parse_response(response)

    async def repair(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(system_prompt, user_prompt)
        return self._parse_response(response)

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> ExtractionResult:
        # DeepSeek's request shape was never bound to a specific schema in
        # the first place (response_format is plain json_object) -- so this
        # is just another _call with a different prompt.
        response = await self._call(system_prompt, user_prompt)
        return self._parse_response(response)

    async def _call(self, system_prompt: str, user_prompt: str) -> dict:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "logprobs": True,
            "top_logprobs": 5,
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        params = {"api-version": self._api_version} if self._api_version else None
        response = await self._client.post(
            f"{self._base_url}/chat/completions", json=payload, headers=headers, params=params
        )
        response.raise_for_status()
        return response.json()

    def _parse_response(self, response: dict) -> ExtractionResult:
        choice = response["choices"][0]
        content = choice["message"]["content"]
        raw_json = json.loads(content)
        usage = response.get("usage", {})
        # DeepSeek's own API names this `prompt_cache_hit_tokens`; Azure's
        # Foundry passthrough may instead surface the OpenAI-compatible
        # `prompt_tokens_details.cached_tokens` shape -- try both rather
        # than assuming either survives the passthrough unchanged.
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return ExtractionResult(
            raw_json=raw_json,
            usage=ExtractionUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cached_input_tokens=cached,
            ),
            logprobs=(choice.get("logprobs") or {}).get("content"),
        )

"""Composite confidence for extracted elements -- UC2's composite adapted
from invoice fields to pii_elements (docs/plan.md D9 + §9).

Why a composite at all: the Day-1 spike measured the model's self-reported
confidence as bimodal (1.0/0.3) and uninformative, while logprobs survive
the Foundry passthrough -- so the stored confidence is assembled from
signals the system can defend, and the model's claim is kept only as one
more signal for disagreement checks (extraction/schemas.py docstring).

Signals (each in [0,1], weighted, renormalized around whatever is
unavailable -- UC2's rule; an absent signal must not silently count as 0
or a tier whose model exposes no logprobs would be structurally penalized
against one that does. Revised 2026-08-12 (docs/plan.md D3 swap): tier 2
is now gpt-5.6-terra, a reasoning model that rejects logprobs outright
(extraction/openai_adapter.py's docstring) -- the exact same signal-loss
SHAPE the retired Anthropic tier 2 already had (it never exposed logprobs
at all), for a different root cause. This mechanism needed no change for
the swap: it already renormalizes around ANY unavailable signal regardless
of provider or reason):

- logprob      -- geometric-mean token probability of the response that
                  produced the element (exp of mean token logprob).
                  Response-level, not per-element: mapping tokens to
                  elements would be guesswork; a hedgy response lowers
                  confidence in ALL its elements, which is the honest read.
- agreement    -- tier-0/tier-1 cross-check: True when the free detectors
                  independently found the same (element_type,
                  value_normalized) in the same passage with a VALID
                  checksum; False when tier-0 saw the value but flagged it
                  (trap context / failed checksum); None when tier 0 has no
                  detector for the type (renormalized away).
- validator    -- the deterministic checksum/structural verdict on the
                  value itself (valid=1.0, format_only=0.5,
                  invalid_checksum=0.0); None for types with no validator.
- grounding    -- D9's string-match: value_raw byte-found in the passage
                  text. Always available; an ungrounded element scores 0
                  here and is review-flagged by the service regardless.

Rollups are MIN, not mean (UC2's rule kept): a flag backed by three
elements is only as trustworthy as its weakest evidence, and averaging
would let two confident elements launder one dubious one.

Repaired cap: an extraction that only validated after the bounded repair
call is capped -- the repair rewrote model output, so its fluency signals
no longer describe the persisted values.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

# Weight per signal. Grounding is weighted lowest not because it matters
# least but because its failure mode is handled harder elsewhere
# (offsets_missing -> review flag); logprob leads because it is the one
# signal measured to carry information on this exact pipeline (D9).
WEIGHTS: dict[str, float] = {
    "logprob": 0.35,
    "agreement": 0.25,
    "validator": 0.25,
    "grounding": 0.15,
}

# Ceiling for elements that only validated after the one bounded repair.
REPAIRED_CAP = 0.75

_VALIDATOR_SCORES = {"valid": 1.0, "format_only": 0.5, "invalid_checksum": 0.0}


@dataclass(frozen=True)
class ElementSignals:
    """Everything composite_confidence weighs, pre-extracted so the caller
    (extraction/service.py) owns all DB/context lookups and this module
    stays pure."""

    grounded: bool
    logprob: float | None = None  # already a probability in [0,1], or None
    tier0_agreement: bool | None = None
    validator_status: str | None = None  # pii_elements.validation_status vocab
    repaired: bool = False


def logprob_signal(logprobs: list[dict] | None) -> float | None:
    """Geometric-mean token probability of a response: exp(mean(logprob)).
    Provider-native token dicts in, one [0,1] scalar out; None when the
    provider/model exposed no logprobs (gpt-5.6-terra, a reasoning model;
    the retired Anthropic tier 2 never exposed them either) or the shape
    is empty -- None, never 0.0, so renormalization can do its job."""
    if not logprobs:
        return None
    values = [
        token["logprob"]
        for token in logprobs
        if isinstance(token, dict) and isinstance(token.get("logprob"), (int, float))
    ]
    if not values:
        return None
    return min(1.0, math.exp(sum(values) / len(values)))


def composite_confidence(signals: ElementSignals) -> float:
    """Weighted mean over the AVAILABLE signals, weights renormalized to
    the available set, repaired cap applied last."""
    scores: dict[str, float] = {"grounding": 1.0 if signals.grounded else 0.0}
    if signals.logprob is not None:
        scores["logprob"] = max(0.0, min(1.0, signals.logprob))
    if signals.tier0_agreement is not None:
        scores["agreement"] = 1.0 if signals.tier0_agreement else 0.0
    if signals.validator_status is not None:
        scores["validator"] = _VALIDATOR_SCORES[signals.validator_status]

    total_weight = sum(WEIGHTS[name] for name in scores)
    value = sum(WEIGHTS[name] * score for name, score in scores.items()) / total_weight
    if signals.repaired:
        value = min(value, REPAIRED_CAP)
    return round(value, 4)


def rollup(confidences: Iterable[float]) -> float:
    """MIN-not-mean rollup (module docstring). Empty input rolls up to 0.0:
    no evidence is the weakest evidence of all."""
    values = list(confidences)
    return min(values) if values else 0.0

"""services/confidence.py unit tests: renormalization around unavailable
signals, the tier-0 agreement boost, the repaired cap, min-not-mean
rollups, and the logprob signal shape. Pure math -- no DB, no adapters."""

from __future__ import annotations

import math

import pytest

from app.services.confidence import (
    REPAIRED_CAP,
    WEIGHTS,
    ElementSignals,
    composite_confidence,
    logprob_signal,
    rollup,
)


def test_all_signals_available_full_agreement_scores_high():
    signals = ElementSignals(
        grounded=True, logprob=0.95, tier0_agreement=True, validator_status="valid"
    )
    value = composite_confidence(signals)
    expected = (
        WEIGHTS["logprob"] * 0.95 + WEIGHTS["agreement"] + WEIGHTS["validator"] + WEIGHTS["grounding"]
    ) / sum(WEIGHTS.values())
    assert value == pytest.approx(expected, abs=1e-4)


def test_unavailable_signals_renormalize_not_zero():
    # The tier-2 reasoning model (gpt-5.6-terra) exposes no logprobs;
    # medical/address have no validator and no tier-0 detector. Grounding
    # alone must then be worth 1.0, not 0.15 -- an absent signal is absent,
    # never a silent zero.
    signals = ElementSignals(grounded=True, logprob=None, tier0_agreement=None, validator_status=None)
    assert composite_confidence(signals) == 1.0


def test_agreement_boosts_over_no_opinion():
    base = dict(grounded=True, logprob=0.9, validator_status=None)
    agreed = composite_confidence(ElementSignals(tier0_agreement=True, **base))
    no_opinion = composite_confidence(ElementSignals(tier0_agreement=None, **base))
    disagreed = composite_confidence(ElementSignals(tier0_agreement=False, **base))
    assert agreed > no_opinion > disagreed


def test_validator_verdicts_order():
    base = dict(grounded=True, logprob=0.9, tier0_agreement=None)
    valid = composite_confidence(ElementSignals(validator_status="valid", **base))
    format_only = composite_confidence(ElementSignals(validator_status="format_only", **base))
    invalid = composite_confidence(ElementSignals(validator_status="invalid_checksum", **base))
    assert valid > format_only > invalid


def test_ungrounded_element_is_penalized():
    grounded = composite_confidence(ElementSignals(grounded=True, logprob=0.9))
    ungrounded = composite_confidence(ElementSignals(grounded=False, logprob=0.9))
    assert ungrounded < grounded


def test_repaired_cap_applies():
    signals = ElementSignals(
        grounded=True, logprob=0.99, tier0_agreement=True, validator_status="valid", repaired=True
    )
    assert composite_confidence(signals) == REPAIRED_CAP
    # ...but the cap is a ceiling, not a floor.
    weak = ElementSignals(grounded=False, logprob=0.2, repaired=True)
    assert composite_confidence(weak) < REPAIRED_CAP


def test_rollup_is_min_not_mean():
    assert rollup([0.9, 0.95, 0.3]) == 0.3
    assert rollup([]) == 0.0


def test_logprob_signal_geometric_mean():
    logprobs = [{"logprob": -0.1}, {"logprob": -0.3}]
    assert logprob_signal(logprobs) == pytest.approx(math.exp(-0.2))
    assert logprob_signal(None) is None
    assert logprob_signal([]) is None
    assert logprob_signal([{"token": "x"}]) is None  # shape without logprob values
    # Never exceeds 1.0 even for pathological positive logprobs.
    assert logprob_signal([{"logprob": 0.5}]) == 1.0

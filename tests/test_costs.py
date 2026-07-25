"""Cost model tests. The headline number must be defensible, so it is tested."""

from __future__ import annotations

import pytest

from overtone.costs import (
    STT_PER_HOUR,
    TTS_PER_1M_CHARS,
    Usage,
    estimate_cost,
)


def test_empty_usage_costs_nothing():
    assert estimate_cost(Usage()).total == 0.0


def test_stt_priced_per_hour():
    usage = Usage()
    usage.add_stt(3600)  # one hour
    assert estimate_cost(usage).stt == pytest.approx(STT_PER_HOUR)


def test_tts_priced_per_million_chars_by_provider():
    usage = Usage()
    usage.add_tts("openai", 1_000_000)
    assert estimate_cost(usage).tts == pytest.approx(TTS_PER_1M_CHARS["openai"])


def test_tts_accumulates_across_calls():
    usage = Usage()
    usage.add_tts("openai", 400)
    usage.add_tts("openai", 600)
    assert usage.tts_chars_by_provider["openai"] == 1000


def test_vision_falls_back_to_per_frame_tokens_when_untracked():
    usage = Usage()
    usage.add_vision(3)  # 3 frames, no token counts
    usage.add_vision(2)
    cost = estimate_cost(usage)
    assert cost.vision > 0  # approximated from frame count


def test_vision_prefers_real_token_counts():
    usage = Usage()
    usage.add_vision(3, input_tokens=5000, output_tokens=100)
    cost = estimate_cost(usage)
    # Input tokens dominate; verify it used the reported count, not the frame
    # approximation (3 * 1100 = 3300 < 5000).
    assert cost.vision == pytest.approx(5000 / 1_000_000 * 0.15 + 100 / 1_000_000 * 0.60)


def test_per_minute_scales_by_media_length():
    usage = Usage()
    usage.add_tts("openai", 1_000_000)  # $15
    cost = estimate_cost(usage)
    # Over 30 minutes of media, $15 is $0.50/min.
    assert cost.per_minute(30 * 60) == pytest.approx(0.5)


def test_per_minute_of_zero_length_is_zero():
    usage = Usage()
    usage.add_tts("openai", 1000)
    assert estimate_cost(usage).per_minute(0) == 0.0


def test_headline_is_cents_not_dollars_for_a_realistic_lecture():
    # Sanity check on the pitch: a 50-minute lecture with ~40 descriptions
    # averaging 90 characters, ~3 frames each, must land well under a dollar,
    # against a human describer's hundreds.
    usage = Usage()
    usage.add_stt(50 * 60)
    for _ in range(40):
        usage.add_vision(3)
        usage.add_tts("openai", 90)
    cost = estimate_cost(usage)
    assert cost.total < 0.50
    assert cost.per_minute(50 * 60) < 0.01

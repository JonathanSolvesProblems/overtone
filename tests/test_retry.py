"""Rate-limit detection and backoff tests."""

from __future__ import annotations

import pytest

from overtone._retry import (
    RATE_LIMIT_FAILOVER_RETRIES,
    RATE_LIMIT_RETRIES,
    is_rate_limit,
    retries_for,
    retry_delay,
)


class TestRetriesFor:
    def test_last_provider_waits_it_out(self):
        assert retries_for(is_last_provider=True) == RATE_LIMIT_RETRIES

    def test_non_last_provider_fails_over_fast(self):
        assert retries_for(is_last_provider=False) == RATE_LIMIT_FAILOVER_RETRIES
        assert RATE_LIMIT_FAILOVER_RETRIES < RATE_LIMIT_RETRIES


class TestIsRateLimit:
    def test_detects_429(self):
        assert is_rate_limit(Exception("Error code: 429 - too many requests"))

    def test_detects_rate_limit_phrase(self):
        assert is_rate_limit(Exception("Rate limit reached for gpt-4o-mini"))
        assert is_rate_limit(Exception("code: rate_limit_exceeded"))

    def test_ignores_unrelated_errors(self):
        assert not is_rate_limit(Exception("401 unauthorized"))
        assert not is_rate_limit(Exception("connection refused"))


class TestRetryDelay:
    def test_honours_millisecond_hint(self):
        exc = Exception("Please try again in 897ms.")
        # 0.897s + 0.25 cushion
        assert retry_delay(exc, 0) == pytest.approx(1.147, abs=0.001)

    def test_honours_second_hint(self):
        exc = Exception("try again in 2s")
        assert retry_delay(exc, 0) == pytest.approx(2.25, abs=0.001)

    def test_falls_back_to_exponential_backoff(self):
        exc = Exception("429 rate limit")  # no explicit hint
        assert retry_delay(exc, 0) == pytest.approx(1.0)
        assert retry_delay(exc, 1) == pytest.approx(2.0)
        assert retry_delay(exc, 2) == pytest.approx(4.0)
        assert retry_delay(exc, 3) == pytest.approx(8.0)

    def test_floor_grows_past_a_short_hint_on_later_attempts(self):
        # A sustained ceiling keeps returning the same optimistic short hint;
        # the growing floor must win on later attempts so we wait long enough.
        exc = Exception("try again in 886ms")
        assert retry_delay(exc, 0) == pytest.approx(1.136, abs=0.01)  # hint wins early
        assert retry_delay(exc, 4) == pytest.approx(16.0)             # floor wins later

    def test_delay_is_capped(self):
        assert retry_delay(Exception("try again in 999s"), 0) == 30.0
        assert retry_delay(Exception("429"), 10) == 30.0

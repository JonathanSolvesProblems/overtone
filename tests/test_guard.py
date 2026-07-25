"""Spend-guard tests using an injected clock (no real time passes)."""

from __future__ import annotations

import pytest

from overtone.guard import (
    Guard,
    GuardConfig,
    NotAllowed,
    RateLimited,
    SpendExceeded,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_guard(**cfg):
    clock = FakeClock()
    guard = Guard(config=GuardConfig(**cfg), now=clock)
    return guard, clock


class TestBudget:
    def test_starts_with_full_budget(self):
        guard, _ = make_guard(daily_spend_cap_usd=5.0)
        assert guard.remaining_budget() == pytest.approx(5.0)

    def test_spend_reduces_budget(self):
        guard, _ = make_guard(daily_spend_cap_usd=5.0)
        guard.record_spend(2.0)
        assert guard.remaining_budget() == pytest.approx(3.0)

    def test_check_budget_raises_when_spent(self):
        guard, _ = make_guard(daily_spend_cap_usd=1.0)
        guard.record_spend(1.0)
        with pytest.raises(SpendExceeded):
            guard.check_budget()

    def test_budget_resets_after_a_day(self):
        guard, clock = make_guard(daily_spend_cap_usd=1.0)
        guard.record_spend(1.0)
        clock.advance(86_401)
        assert guard.remaining_budget() == pytest.approx(1.0)


class TestRate:
    def test_allows_up_to_the_limit(self):
        guard, _ = make_guard(per_client_per_hour=3)
        for _ in range(3):
            guard.check_rate("ip-1")

    def test_blocks_over_the_limit(self):
        guard, _ = make_guard(per_client_per_hour=2)
        guard.check_rate("ip-1")
        guard.check_rate("ip-1")
        with pytest.raises(RateLimited):
            guard.check_rate("ip-1")

    def test_clients_are_independent(self):
        guard, _ = make_guard(per_client_per_hour=1)
        guard.check_rate("ip-1")
        guard.check_rate("ip-2")  # different client, still allowed

    def test_window_slides(self):
        guard, clock = make_guard(per_client_per_hour=1)
        guard.check_rate("ip-1")
        clock.advance(3601)
        guard.check_rate("ip-1")  # previous hit aged out


class TestAllowlist:
    def test_allowed_prefix_passes(self):
        guard, _ = make_guard(allowed_prefixes=("demo/",))
        guard.check_allowed("demo/lecture.mp4")

    def test_other_prefix_is_rejected(self):
        guard, _ = make_guard(allowed_prefixes=("demo/",))
        with pytest.raises(NotAllowed):
            guard.check_allowed("private/secret.mp4")

    def test_duration_cap_enforced(self):
        guard, _ = make_guard(max_video_seconds=60)
        with pytest.raises(NotAllowed):
            guard.check_allowed("demo/x.mp4", media_seconds=120)

    def test_duration_within_cap_passes(self):
        guard, _ = make_guard(max_video_seconds=60)
        guard.check_allowed("demo/x.mp4", media_seconds=45)


def test_authorize_runs_all_cheap_gates():
    guard, _ = make_guard(daily_spend_cap_usd=5.0, per_client_per_hour=2)
    guard.authorize("ip-1", "demo/x.mp4")
    guard.authorize("ip-1", "demo/x.mp4")
    with pytest.raises(RateLimited):
        guard.authorize("ip-1", "demo/x.mp4")


def test_authorize_rejects_disallowed_key_before_rate():
    guard, _ = make_guard()
    with pytest.raises(NotAllowed):
        guard.authorize("ip-1", "elsewhere/x.mp4")

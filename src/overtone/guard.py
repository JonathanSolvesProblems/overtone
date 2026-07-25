"""Abuse and spend protection for the public web app.

The hosted demo runs real provider calls against a real API key on a public URL,
so it needs guard rails a private CLI does not. Three of them:

- a daily spend ceiling, so a busy day cannot run up an open-ended bill;
- a per-client rate limit, so one visitor cannot monopolise the budget;
- an allowlist of demo videos with a duration cap, so a live describe can only
  ever run on curated, short clips rather than an arbitrary upload.

State is deliberately in-memory and process-local. A single small instance is
all the demo needs, and losing the counters on restart fails safe (the ceiling
resets, it does not vanish). None of this touches the CLI or library paths.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardConfig:
    """Tunable limits for the hosted demo."""

    daily_spend_cap_usd: float = 5.0
    per_client_per_hour: int = 4
    max_video_seconds: float = 360.0
    # Only keys matching one of these prefixes may be described live.
    allowed_prefixes: tuple[str, ...] = ("demo/",)


class SpendExceeded(Exception):
    """The daily spend ceiling has been reached."""


class RateLimited(Exception):
    """This client has made too many requests in the window."""


class NotAllowed(Exception):
    """The requested video is outside the demo allowlist or too long."""


@dataclass
class Guard:
    """Thread-safe gatekeeper for live describe requests.

    A ``now`` callable is injected so the limits can be tested deterministically
    without real time passing.
    """

    config: GuardConfig = field(default_factory=GuardConfig)
    now: object = None  # Callable[[], float]; defaults to time.monotonic

    def __post_init__(self) -> None:
        if self.now is None:
            import time

            self.now = time.monotonic
        self._lock = threading.Lock()
        self._spent_today = 0.0
        self._day_started = self.now()
        self._client_hits: dict[str, deque] = {}

    # -- spend ---------------------------------------------------------------

    def _roll_day(self) -> None:
        if self.now() - self._day_started >= 86_400:
            self._spent_today = 0.0
            self._day_started = self.now()

    def remaining_budget(self) -> float:
        with self._lock:
            self._roll_day()
            return max(0.0, self.config.daily_spend_cap_usd - self._spent_today)

    def record_spend(self, usd: float) -> None:
        with self._lock:
            self._roll_day()
            self._spent_today += max(0.0, usd)

    def check_budget(self) -> None:
        if self.remaining_budget() <= 0:
            raise SpendExceeded(
                f"Daily demo budget of ${self.config.daily_spend_cap_usd:.0f} is spent. "
                "Try again tomorrow, or run it yourself from the repo."
            )

    # -- rate limiting -------------------------------------------------------

    def check_rate(self, client_id: str) -> None:
        window = 3600.0
        with self._lock:
            now = self.now()
            hits = self._client_hits.setdefault(client_id, deque())
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= self.config.per_client_per_hour:
                raise RateLimited(
                    f"Rate limit reached ({self.config.per_client_per_hour}/hour). "
                    "Give the demo a breather, or run it from the repo."
                )
            hits.append(now)

    # -- allowlist -----------------------------------------------------------

    def check_allowed(self, key: str, *, media_seconds: float | None = None) -> None:
        if not any(key.startswith(p) for p in self.config.allowed_prefixes):
            raise NotAllowed(
                "Live describe is limited to the demo videos. "
                "Point the CLI at your own bucket to describe anything."
            )
        if media_seconds is not None and media_seconds > self.config.max_video_seconds:
            raise NotAllowed(
                f"That video is longer than the {self.config.max_video_seconds/60:.0f}-minute "
                "live-demo cap. The CLI has no such limit."
            )

    def authorize(self, client_id: str, key: str) -> None:
        """Run the cheap gates before a describe. Duration is checked later."""
        self.check_budget()
        self.check_allowed(key)
        self.check_rate(client_id)

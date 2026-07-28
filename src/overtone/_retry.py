"""Shared rate-limit retry helpers.

Archive-scale runs hit provider tokens-per-minute ceilings routinely. The right
response is to wait the moment the provider asks for and try again, not to
abandon the run or immediately burn a fallback provider. Vision and TTS both
use these.

Note: genblaze added opt-in rate-limit backoff to the chat()/vision helpers in
PR #229 (from issue #221, filed while building this). Once that lands on PyPI,
the per-provider retry here can defer to it — but pass the SDK flag *or* keep
this loop, never both, to avoid double backoff.
"""

from __future__ import annotations

import re

# Retries on the LAST provider in a chain: six escalating waits can span a full
# tokens-per-minute window, so a sustained ceiling is waited out rather than
# abandoned (there is nowhere else to go).
RATE_LIMIT_RETRIES = 6

# Retries before failing over when another provider is still available: just one
# quick attempt, then spill to the next provider. A rate-limited free-tier key
# should hand off in a second or two, not stall a live request waiting out its
# window when a paid fallback is sitting right there.
RATE_LIMIT_FAILOVER_RETRIES = 1


def retries_for(is_last_provider: bool) -> int:
    """How many rate-limit retries to allow before giving up on a provider."""
    return RATE_LIMIT_RETRIES if is_last_provider else RATE_LIMIT_FAILOVER_RETRIES

# Hard ceiling on any single wait.
_MAX_DELAY = 30.0

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)\s*(ms|s)", re.IGNORECASE)


def is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def retry_delay(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying a rate-limited call.

    Takes the larger of the provider's own "try again in N" hint and an
    exponential floor that grows with each attempt. A provider under a sustained
    per-minute ceiling keeps returning the same optimistic short hint, so
    honouring it alone would retry too fast to ever clear the window; the
    growing floor guarantees later attempts wait long enough. Capped at
    :data:`_MAX_DELAY`.
    """
    floor = min(_MAX_DELAY, 2.0**attempt)  # 1, 2, 4, 8, 16, 30, 30, ...
    hinted = 0.0
    m = _RETRY_AFTER_RE.search(str(exc))
    if m:
        value = float(m.group(1))
        hinted = (value / 1000.0 if m.group(2).lower() == "ms" else value) + 0.25
    return min(_MAX_DELAY, max(floor, hinted))

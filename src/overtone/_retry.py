"""Shared rate-limit retry helpers.

Archive-scale runs hit provider tokens-per-minute ceilings routinely. The right
response is to wait the moment the provider asks for and try again, not to
abandon the run or immediately burn a fallback provider. Vision and TTS both
use these.
"""

from __future__ import annotations

import re

# Retries per provider on a rate-limit response before failing over. Six
# escalating waits can span a full tokens-per-minute window, so a sustained
# ceiling (not just a transient spike) is waited out rather than abandoned.
RATE_LIMIT_RETRIES = 6

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

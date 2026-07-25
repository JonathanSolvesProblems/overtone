"""Usage accounting and cost estimation.

The headline claim of this project is a price per finished minute, set against a
human describer's $15 to $75. That number has to be honest, so it is computed
from metered usage against published list prices, not asserted. Every provider
call adds to a :class:`Usage` tally; :func:`estimate_cost` turns the tally into
dollars using the rates below.

Rates are list prices as of mid-2026 and are kept in one table so they are easy
to audit and update. They are intentionally conservative (list, not the volume
discounts an archive-scale customer would negotiate), which means the real
figure only moves in our favour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Published list prices (USD) --------------------------------------------
# AssemblyAI speech-to-text, per hour of audio.
STT_PER_HOUR = 0.12

# OpenAI, per 1M tokens. gpt-4o-mini vision input is billed as tokens; image
# tiles add tokens we approximate per frame below.
VISION_INPUT_PER_1M = 0.15
VISION_OUTPUT_PER_1M = 0.60
# A downscaled 768px frame is on the order of ~1.1k tokens to gpt-4o-mini.
VISION_TOKENS_PER_FRAME = 1_100

# Text-to-speech, per 1M characters.
TTS_PER_1M_CHARS = {
    "openai": 15.0,        # tts-1
    "elevenlabs": 100.0,   # roughly, on a mid creator tier; free tier is $0
    "hume": 30.0,          # Octave, approximate
}

# B2 storage, per GB-month, and egress. Egress is what makes the in-bucket
# design cheap: describing in place avoids the download-to-vendor round trip.
B2_STORAGE_PER_GB_MONTH = 0.006

# The human baseline the headline is measured against. Professional audio
# description runs $15 to $75 per finished minute depending on complexity
# (3Play Media, Verbit, industry surveys, mid-2026). These are the numbers a
# university actually pays, and the comparison the pitch turns on.
HUMAN_AD_PER_MINUTE_LOW = 15.0
HUMAN_AD_PER_MINUTE_HIGH = 75.0


@dataclass
class Usage:
    """A running tally of what a describe run consumed."""

    stt_seconds: float = 0.0
    vision_calls: int = 0
    vision_frames: int = 0
    vision_input_tokens: int = 0
    vision_output_tokens: int = 0
    tts_chars_by_provider: dict[str, int] = field(default_factory=dict)

    def add_stt(self, seconds: float) -> None:
        self.stt_seconds += seconds

    def add_vision(self, frames: int, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.vision_calls += 1
        self.vision_frames += frames
        self.vision_input_tokens += input_tokens
        self.vision_output_tokens += output_tokens

    def add_tts(self, provider: str, chars: int) -> None:
        self.tts_chars_by_provider[provider] = (
            self.tts_chars_by_provider.get(provider, 0) + chars
        )


@dataclass(frozen=True)
class CostBreakdown:
    """Estimated cost of a run, itemized so the total can be trusted."""

    stt: float
    vision: float
    tts: float

    @property
    def total(self) -> float:
        return self.stt + self.vision + self.tts

    def per_minute(self, media_seconds: float) -> float:
        if media_seconds <= 0:
            return 0.0
        return self.total / (media_seconds / 60.0)

    def as_dict(self) -> dict:
        return {"stt": self.stt, "vision": self.vision, "tts": self.tts, "total": self.total}


def estimate_cost(usage: Usage) -> CostBreakdown:
    """Turn a usage tally into an itemized dollar estimate."""
    stt = (usage.stt_seconds / 3600.0) * STT_PER_HOUR

    # Prefer real token counts when the provider reported them; otherwise fall
    # back to a per-frame token approximation so a run still gets a number.
    input_tokens = usage.vision_input_tokens or (usage.vision_frames * VISION_TOKENS_PER_FRAME)
    vision = (
        input_tokens / 1_000_000 * VISION_INPUT_PER_1M
        + usage.vision_output_tokens / 1_000_000 * VISION_OUTPUT_PER_1M
    )

    tts = 0.0
    for provider, chars in usage.tts_chars_by_provider.items():
        rate = TTS_PER_1M_CHARS.get(provider, 15.0)
        tts += chars / 1_000_000 * rate

    return CostBreakdown(stt=stt, vision=vision, tts=tts)

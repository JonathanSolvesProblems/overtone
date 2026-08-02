"""Runtime configuration, loaded from the environment.

One place that knows which environment variables exist, so the rest of the
codebase asks for a typed object instead of reaching into ``os.environ``.
Provider keys are read lazily by their connectors; this only owns the settings
Overtone itself needs to make decisions (which bucket, which model chain).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from overtone.synth import TTSVoice
from overtone.vision import VisionModel


def _load_dotenv(path: str | Path = ".env") -> None:
    """Populate ``os.environ`` from a .env file, without overwriting.

    Deliberately tiny and dependency-free. Existing environment variables win,
    so a real deployment's injected secrets are never clobbered by a stray
    committed file.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class B2Config:
    """Everything needed to reach the Backblaze bucket."""

    bucket: str
    endpoint: str
    region: str
    key_id: str
    app_key: str

    @property
    def endpoint_url(self) -> str:
        if self.endpoint.startswith("http"):
            return self.endpoint
        return f"https://{self.endpoint}"


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for a run."""

    b2: B2Config | None
    vision_chain: list[VisionModel]
    tts_chain: list[TTSVoice]
    stt_model: str = "universal-2"
    has_assemblyai: bool = False
    _present: set[str] = field(default_factory=set)

    def has(self, var: str) -> bool:
        return var in self._present


# Default model per provider. Kept here rather than scattered through the code
# so a model bump is a one-line change.
# "gemini-flash-latest" is a stable alias that tracks Google's current Flash
# model, so it keeps working as specific versions are retired (a pinned
# gemini-2.5-flash now 404s for new keys). Vision-capable and free-tier eligible.
_VISION_DEFAULTS = {
    "google": "gemini-flash-latest",
    "openai": "gpt-4o",
    "gmicloud": "Qwen/Qwen2.5-VL-7B-Instruct",
}

# Vision providers are tried in this order when their keys are present. OpenAI
# leads because it is the reliable, higher-limit path for a live request; the
# free Gemini tier sits behind it as a no-cost fallback that also absorbs
# spillover when OpenAI hits its own per-minute ceiling on a big run.
_VISION_PRIORITY = ("openai", "google", "gmicloud")


def _present_vars() -> set[str]:
    keys = (
        "B2_KEY_ID",
        "B2_APPLICATION_KEY",
        "ASSEMBLYAI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "GMI_CLOUD_API_KEY",
        "HUME_API_KEY",
        "ELEVENLABS_API_KEY",
    )
    return {k for k in keys if os.environ.get(k)}


def _build_vision_chain(present: set[str]) -> list[VisionModel]:
    """Order the available vision providers into a failover chain."""
    var_for = {
        "google": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gmicloud": "GMI_CLOUD_API_KEY",
    }
    chain: list[VisionModel] = []
    for provider in _VISION_PRIORITY:
        if var_for[provider] in present:
            chain.append(VisionModel(provider=provider, model=_VISION_DEFAULTS[provider]))
    return chain


# "River — Relaxed, Neutral, Informative", a premade ElevenLabs voice (so it
# works on the free tier) whose register matches the WCAG-recommended neutral
# description style and stays distinct from a lecturer's own voice.
DEFAULT_ELEVENLABS_VOICE = "SAz9YHcvj6GT2YYXdXww"

# OpenAI's clearest neutral narrator among the stock voices.
DEFAULT_OPENAI_VOICE = "onyx"


def _build_tts_chain(present: set[str]) -> list[TTSVoice]:
    """Order the available TTS providers into a failover chain.

    ElevenLabs leads on voice quality; OpenAI TTS backs it up and shares the
    key that already drives vision, so the fallback works even on a bare setup.
    Hume Octave takes the lead when its key is present.
    """
    chain: list[TTSVoice] = []
    if "HUME_API_KEY" in present:
        chain.append(TTSVoice(provider="hume", model="octave", voice=None))
    if "ELEVENLABS_API_KEY" in present:
        chain.append(
            TTSVoice(
                provider="elevenlabs",
                model="eleven_multilingual_v2",
                voice=DEFAULT_ELEVENLABS_VOICE,
            )
        )
    if "OPENAI_API_KEY" in present:
        chain.append(TTSVoice(provider="openai", model="tts-1", voice=DEFAULT_OPENAI_VOICE))
    return chain


def load(dotenv: str | Path | None = ".env") -> Settings:
    """Load settings from the environment (and an optional .env file)."""
    if dotenv is not None:
        _load_dotenv(dotenv)

    present = _present_vars()

    b2: B2Config | None = None
    # Accept both spellings of the app-key var: genblaze's for_backblaze reads
    # B2_APP_KEY, but B2_APPLICATION_KEY reads more naturally and is what the
    # B2 console labels it. Support both so neither surprises the next person.
    app_key = os.environ.get("B2_APPLICATION_KEY") or os.environ.get("B2_APP_KEY")
    if os.environ.get("B2_KEY_ID") and app_key and os.environ.get("B2_BUCKET"):
        b2 = B2Config(
            bucket=os.environ["B2_BUCKET"],
            endpoint=os.environ.get("B2_ENDPOINT", "s3.us-west-004.backblazeb2.com"),
            region=os.environ.get("B2_REGION", "us-west-004"),
            key_id=os.environ["B2_KEY_ID"],
            app_key=app_key,
        )

    return Settings(
        b2=b2,
        vision_chain=_build_vision_chain(present),
        tts_chain=_build_tts_chain(present),
        has_assemblyai="ASSEMBLYAI_API_KEY" in present,
        _present=present,
    )

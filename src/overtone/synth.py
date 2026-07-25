"""Text-to-speech with provider failover, returning measured audio.

Every TTS connector in Genblaze writes a local file and reports a duration, but
the reported duration depends on an optional dependency and on each vendor's
own rounding. The fit loop cannot afford to be wrong about how long a clip
runs, so this module always remeasures the rendered file with ffprobe and
treats that as ground truth.

Failover matters here for the same reason it does for vision: a run across an
archive will exhaust a voice quota or hit a rate limit, and moving to the next
provider costs a retry rather than the batch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata
from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from overtone.ffmpeg import audio_duration

logger = logging.getLogger("overtone.synth")


@dataclass(frozen=True)
class TTSVoice:
    """One provider, model, and voice to try when speaking a description."""

    provider: str
    model: str
    voice: str | None

    def __str__(self) -> str:
        voice = f"/{self.voice}" if self.voice else ""
        return f"{self.provider}:{self.model}{voice}"


@dataclass(frozen=True)
class SpokenClip:
    """A rendered description clip and its measured length."""

    path: Path
    seconds: float
    voice: TTSVoice
    cost_usd: float | None = None
    attempts: list[str] = None


def _asset_local_path(url: str) -> Path:
    """Resolve a provider's ``file://`` asset URL to a filesystem path."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise ProviderError(f"Expected a local file:// audio asset, got {url!r}")
    return Path(url2pathname(parsed.path))


def _make_step(voice: TTSVoice, text: str, output_format: str) -> Step:
    """Build the provider Step for one TTS call.

    Each connector names its knobs differently (``voice_id`` vs ``voice``,
    ``output_format`` vs ``response_format``); this is the one place that
    difference lives.
    """
    step = Step(provider=voice.provider, model=voice.model, modality=Modality.AUDIO)
    step.prompt = text

    if voice.provider == "elevenlabs":
        if voice.voice:
            step.params["voice_id"] = voice.voice
        step.params["output_format"] = {
            "mp3": "mp3_44100_128",
            "wav": "pcm_44100",
        }.get(output_format, "mp3_44100_128")
    elif voice.provider == "openai":
        if voice.voice:
            step.params["voice"] = voice.voice
        step.params["response_format"] = output_format
    elif voice.provider == "hume":
        if voice.voice:
            step.params["voice"] = voice.voice
        step.params["format"] = output_format
    else:
        raise ValueError(f"Unknown TTS provider: {voice.provider}")

    return step


def _provider_for(voice: TTSVoice):
    """Instantiate the connector for a provider (imported lazily)."""
    if voice.provider == "elevenlabs":
        from genblaze_elevenlabs import ElevenLabsTTSProvider

        return ElevenLabsTTSProvider()
    if voice.provider == "openai":
        from genblaze_openai import OpenAITTSProvider

        return OpenAITTSProvider()
    if voice.provider == "hume":
        from genblaze_hume import HumeTTSProvider

        return HumeTTSProvider()
    raise ValueError(f"Unknown TTS provider: {voice.provider}")


def speak(
    text: str,
    chain: list[TTSVoice],
    *,
    output_format: str = "mp3",
) -> SpokenClip:
    """Synthesize ``text`` to speech, falling through ``chain`` on failure.

    Returns the rendered clip with its remeasured duration. Raises only when
    every provider in the chain fails.
    """
    if not chain:
        raise ValueError("speak() requires at least one TTSVoice")
    if not text.strip():
        raise ValueError("speak() requires non-empty text")

    tried: list[str] = []
    last_error: Exception | None = None

    for voice in chain:
        try:
            provider = _provider_for(voice)
            step = _make_step(voice, text, output_format)
            provider.generate(step)
            asset = step.assets[0]
            path = _asset_local_path(asset.url)
            seconds = audio_duration(path)
        except (ProviderError, Exception) as exc:  # noqa: BLE001
            logger.warning("tts provider %s failed: %s", voice, str(exc)[:200])
            tried.append(str(voice))
            last_error = exc
            continue

        return SpokenClip(
            path=path,
            seconds=seconds,
            voice=voice,
            cost_usd=step.cost_usd,
            attempts=tried,
        )

    raise ProviderError(
        f"All TTS providers failed ({', '.join(tried)}): {last_error}"
    ) from last_error


class FailoverTTSProvider(SyncProvider):
    """A Genblaze provider that speaks via a failover chain.

    Wrapping :func:`speak` as a single ``SyncProvider`` lets the fit loop stay a
    genuine Genblaze pipeline step while still getting cross-provider failover
    underneath. The rendered clip is attached as a ``file://`` audio asset with
    its measured duration, and an optional callback reports the provider and
    character count for cost accounting.
    """

    name = "overtone-failover-tts"

    def __init__(
        self,
        chain: list[TTSVoice],
        *,
        output_format: str = "mp3",
        on_synth: Callable[[str, int], None] | None = None,
    ):
        super().__init__()
        self._chain = chain
        self._output_format = output_format
        self._on_synth = on_synth
        self.last_clip: SpokenClip | None = None

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.AUDIO],
            output_formats=["audio/mpeg", "audio/wav"],
        )

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        text = step.prompt or ""
        clip = speak(text, self._chain, output_format=self._output_format)
        self.last_clip = clip
        if self._on_synth is not None:
            self._on_synth(clip.voice.provider, len(text))

        asset = Asset(url=local_file_url(clip.path.resolve()), media_type="audio/mpeg")
        asset.duration = clip.seconds
        asset.audio = AudioMetadata(codec="mp3", channels=1)
        if clip.cost_usd is not None:
            step.cost_usd = clip.cost_usd
        step.assets.append(asset)
        return step

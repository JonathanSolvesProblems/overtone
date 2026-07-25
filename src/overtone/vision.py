"""Provider-agnostic multimodal description calls.

Genblaze's promise is that you swap providers without rewriting orchestration,
and for text-only chat that holds. For vision it does not, so this module
absorbs the difference.

The canonical ``ImageURLContent`` block works on OpenAI-wire connectors
(OpenAI, GMI Cloud). ``genblaze-google`` rejects it, and the raw-dict workaround
its own error message recommends is rejected identically. Gemini vision is
reachable, but only through a raw dict in Gemini's native ``parts`` /
``inline_data`` shape. Verified against genblaze-google 0.3.3 and filed
upstream.

Keeping that knowledge here means the rest of Overtone asks for a description
and never learns which vendor answered.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import (
    ChatMessage,
    ImageURLContent,
    ImageURLRef,
    TextContent,
)

from overtone._retry import RATE_LIMIT_RETRIES, is_rate_limit, retry_delay

logger = logging.getLogger("overtone.vision")

# Connectors whose chat() speaks the OpenAI wire shape and therefore accept
# the canonical typed content blocks.
OPENAI_WIRE = frozenset({"openai", "gmicloud"})


@dataclass(frozen=True)
class VisionModel:
    """One provider and model to try when describing frames."""

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class VisionResult:
    """A description plus which provider actually produced it."""

    text: str
    model: VisionModel
    tokens_in: int | None = None
    tokens_out: int | None = None
    attempts: list[str] = None  # providers tried before this one succeeded


def encode_image(path: str | Path) -> tuple[str, str]:
    """Return ``(mime_type, base64_payload)`` for an image file."""
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def build_openai_messages(frames: list[Path], prompt: str) -> list[ChatMessage]:
    """Build a typed multimodal turn for OpenAI-wire connectors."""
    blocks: list = [TextContent(text=prompt)]
    for frame in frames:
        mime, payload = encode_image(frame)
        blocks.append(
            ImageURLContent(image_url=ImageURLRef(url=f"data:{mime};base64,{payload}"))
        )
    return [ChatMessage(role="user", content=blocks)]


def build_gemini_messages(frames: list[Path], prompt: str) -> list[dict]:
    """Build a raw Gemini turn using native ``parts`` and ``inline_data``.

    Deliberately a raw dict. Passing typed blocks, or the OpenAI-shaped raw
    dict the connector's error message suggests, both raise before any request
    is made.
    """
    parts: list[dict] = [{"text": prompt}]
    for frame in frames:
        mime, payload = encode_image(frame)
        parts.append({"inline_data": {"mime_type": mime, "data": payload}})
    return [{"role": "user", "parts": parts}]


def _call(
    model: VisionModel,
    frames: list[Path],
    system: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
):
    """Dispatch one provider call, translating to that provider's shape."""
    if model.provider == "google":
        from genblaze_google import chat as google_chat

        return google_chat(
            model.model,
            messages=build_gemini_messages(frames, prompt),
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    if model.provider == "openai":
        from genblaze_openai import chat as openai_chat

        return openai_chat(
            model.model,
            messages=build_openai_messages(frames, prompt),
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    if model.provider == "gmicloud":
        from genblaze_gmicloud import chat as gmi_chat

        return gmi_chat(
            model.model,
            messages=build_openai_messages(frames, prompt),
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    raise ValueError(f"Unknown vision provider: {model.provider}")


def describe(
    frames: list[Path],
    system: str,
    prompt: str,
    chain: list[VisionModel],
    *,
    max_tokens: int = 300,
    temperature: float = 0.4,
) -> VisionResult:
    """Describe frames, falling through the chain on provider failure.

    Failover here is not decoration. Vision endpoints rate-limit and time out,
    and a run over an archive of thousands of videos will hit both. Moving to
    the next provider costs a retry; failing the run costs the batch.
    """
    if not chain:
        raise ValueError("describe() requires at least one VisionModel")

    tried: list[str] = []
    last_error: Exception | None = None

    for model in chain:
        response = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = _call(
                    model,
                    frames,
                    system,
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                break
            except (ProviderError, Exception) as exc:  # noqa: BLE001
                if is_rate_limit(exc) and attempt < RATE_LIMIT_RETRIES:
                    delay = retry_delay(exc, attempt)
                    logger.info("vision %s rate-limited; waiting %.2fs", model, delay)
                    time.sleep(delay)
                    continue
                logger.warning("vision provider %s failed: %s", model, str(exc)[:200])
                tried.append(str(model))
                last_error = exc
                break

        if response is None:
            continue

        text = (response.text or "").strip()
        if not text:
            logger.warning("vision provider %s returned empty text", model)
            tried.append(str(model))
            continue

        return VisionResult(
            text=text,
            model=model,
            tokens_in=getattr(response, "tokens_in", None),
            tokens_out=getattr(response, "tokens_out", None),
            attempts=tried,
        )

    raise ProviderError(
        f"All vision providers failed ({', '.join(tried)}): {last_error}"
    ) from last_error

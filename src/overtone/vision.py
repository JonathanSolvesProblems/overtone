"""Provider-agnostic multimodal description calls.

Genblaze's promise is that you swap vision providers without rewriting
orchestration, and as of genblaze-google 0.3.4 that finally holds for images
too: every provider here takes the same canonical ``ImageURLContent`` blocks
through its ``chat()`` helper. Google reaching parity came from a bug this
project filed (genblaze #194, shipped as PR #217), so the one path below now
works for OpenAI, Gemini, and GMI Cloud alike.

Keeping that behind one function means the rest of Overtone asks for a
description and never learns which vendor answered.
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

from overtone._retry import is_rate_limit, retries_for, retry_delay

logger = logging.getLogger("overtone.vision")


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


def build_messages(frames: list[Path], prompt: str) -> list[ChatMessage]:
    """Build a canonical multimodal turn: a text block plus one image per frame.

    The same typed ``ImageURLContent`` blocks are accepted by every vision
    connector now, so there is one message shape rather than one per vendor.
    """
    blocks: list = [TextContent(text=prompt)]
    for frame in frames:
        mime, payload = encode_image(frame)
        blocks.append(
            ImageURLContent(image_url=ImageURLRef(url=f"data:{mime};base64,{payload}"))
        )
    return [ChatMessage(role="user", content=blocks)]


def _chat_for(provider: str):
    """Return the Genblaze ``chat()`` helper for a vision provider."""
    if provider == "openai":
        from genblaze_openai import chat

        return chat
    if provider == "google":
        from genblaze_google import chat

        return chat
    if provider == "gmicloud":
        from genblaze_gmicloud import chat

        return chat
    raise ValueError(f"Unknown vision provider: {provider}")


def _call(
    model: VisionModel,
    frames: list[Path],
    system: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
):
    """Describe frames with one provider, via its Genblaze ``chat()`` helper.

    Every provider takes the identical canonical message; the only per-provider
    knowledge left is which ``chat()`` to import.
    """
    chat = _chat_for(model.provider)
    return chat(
        model.model,
        messages=build_messages(frames, prompt),
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
    )


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

    for index, model in enumerate(chain):
        max_retries = retries_for(is_last_provider=index == len(chain) - 1)
        response = None
        for attempt in range(max_retries + 1):
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
                if is_rate_limit(exc) and attempt < max_retries:
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

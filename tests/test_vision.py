"""Multimodal message-shape and failover tests.

Message building runs offline. The failover tests use fakes rather than live
providers, so the retry policy is verified without spending anything.
"""

from __future__ import annotations

import base64

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.chat import ChatMessage, ImageURLContent, TextContent

from overtone import vision
from overtone.vision import (
    VisionModel,
    build_gemini_messages,
    build_openai_messages,
    describe,
    encode_image,
)

PIXEL = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy/8AAEQgAAQABAwEiAAIR"
    "AQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAAB"
    "fQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeo"
    "qaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/aAAwDAQAC"
    "EQMRAD8A/fyiiigD/9k="
)


@pytest.fixture
def frames(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(PIXEL)
        paths.append(p)
    return paths


class TestEncoding:
    def test_returns_mime_and_base64(self, frames):
        mime, payload = encode_image(frames[0])
        assert mime == "image/jpeg"
        assert base64.b64decode(payload) == PIXEL

    def test_unknown_extension_defaults_to_jpeg(self, tmp_path):
        odd = tmp_path / "frame.weird"
        odd.write_bytes(PIXEL)
        assert encode_image(odd)[0] == "image/jpeg"


class TestOpenAIShape:
    def test_prompt_comes_first(self, frames):
        blocks = build_openai_messages(frames, "Describe.")[0].content
        assert isinstance(blocks[0], TextContent)
        assert blocks[0].text == "Describe."

    def test_every_frame_becomes_an_image_block(self, frames):
        blocks = build_openai_messages(frames, "Describe.")[0].content
        images = [b for b in blocks if isinstance(b, ImageURLContent)]
        assert len(images) == len(frames)

    def test_images_are_data_uris(self, frames):
        blocks = build_openai_messages(frames, "Describe.")[0].content
        image = next(b for b in blocks if isinstance(b, ImageURLContent))
        assert image.image_url.url.startswith("data:image/jpeg;base64,")

    def test_returns_a_single_user_turn(self, frames):
        messages = build_openai_messages(frames, "Describe.")
        assert len(messages) == 1
        assert isinstance(messages[0], ChatMessage)
        assert messages[0].role == "user"


class TestGeminiShape:
    def test_uses_raw_dicts_not_chat_messages(self, frames):
        # Typed blocks are rejected by genblaze-google, so this must stay raw.
        messages = build_gemini_messages(frames, "Describe.")
        assert isinstance(messages[0], dict)

    def test_uses_native_parts_and_inline_data(self, frames):
        parts = build_gemini_messages(frames, "Describe.")[0]["parts"]
        assert parts[0] == {"text": "Describe."}
        assert set(parts[1]["inline_data"]) == {"mime_type", "data"}

    def test_inline_data_is_bare_base64_not_a_data_uri(self, frames):
        # Gemini wants the payload alone; a data: prefix silently breaks it.
        payload = build_gemini_messages(frames, "Describe.")[0]["parts"][1]["inline_data"]["data"]
        assert not payload.startswith("data:")
        assert base64.b64decode(payload) == PIXEL

    def test_one_part_per_frame_plus_the_prompt(self, frames):
        parts = build_gemini_messages(frames, "Describe.")[0]["parts"]
        assert len(parts) == len(frames) + 1


class FakeResponse:
    def __init__(self, text, tokens_in=10, tokens_out=5):
        self.text = text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class TestFailover:
    def test_requires_a_chain(self, frames):
        with pytest.raises(ValueError):
            describe(frames, "sys", "prompt", [])

    def test_returns_the_first_success(self, frames, monkeypatch):
        monkeypatch.setattr(vision, "_call", lambda m, *a, **k: FakeResponse("A red slide."))
        result = describe(frames, "sys", "prompt", [VisionModel("gmicloud", "m1")])
        assert result.text == "A red slide."
        assert result.model.provider == "gmicloud"

    def test_falls_through_to_the_next_provider(self, frames, monkeypatch):
        def flaky(model, *args, **kwargs):
            if model.provider == "gmicloud":
                raise ProviderError("rate limited")
            return FakeResponse("A bar chart rising left to right.")

        monkeypatch.setattr(vision, "_call", flaky)
        result = describe(
            frames,
            "sys",
            "prompt",
            [VisionModel("gmicloud", "m1"), VisionModel("openai", "gpt-4o-mini")],
        )
        assert result.model.provider == "openai"
        assert result.attempts == ["gmicloud:m1"]

    def test_empty_text_is_treated_as_failure(self, frames, monkeypatch):
        def blank_then_good(model, *args, **kwargs):
            if model.provider == "gmicloud":
                return FakeResponse("   ")
            return FakeResponse("A diagram of three connected nodes.")

        monkeypatch.setattr(vision, "_call", blank_then_good)
        result = describe(
            frames,
            "sys",
            "prompt",
            [VisionModel("gmicloud", "m1"), VisionModel("openai", "m2")],
        )
        assert result.model.provider == "openai"

    def test_raises_when_every_provider_fails(self, frames, monkeypatch):
        def always_fail(*args, **kwargs):
            raise ProviderError("down")

        monkeypatch.setattr(vision, "_call", always_fail)
        with pytest.raises(ProviderError, match="All vision providers failed"):
            describe(
                frames,
                "sys",
                "prompt",
                [VisionModel("gmicloud", "m1"), VisionModel("openai", "m2")],
            )

    def test_response_text_is_stripped(self, frames, monkeypatch):
        monkeypatch.setattr(vision, "_call", lambda *a, **k: FakeResponse("  padded  "))
        result = describe(frames, "sys", "prompt", [VisionModel("openai", "m")])
        assert result.text == "padded"


def test_unknown_provider_is_rejected(frames):
    with pytest.raises(ValueError, match="Unknown vision provider"):
        vision._call(
            VisionModel("nope", "m"), frames, "sys", "prompt", max_tokens=10, temperature=0.1
        )

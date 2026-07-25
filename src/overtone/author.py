"""The description author: turns frames plus context into fitted text.

This is the ``describe_fn`` the fit loop calls. It holds everything that stays
constant across a gap's retries (the frames, the surrounding transcript, the
descriptions already written) and varies only the word budget and the feedback,
so a retry writes a shorter description of the same moment rather than starting
over.

Keeping this separate from the loop is what let the loop be tested against mocks
with no keys: the loop knows nothing about vision models, only that something
turns a budget into text.
"""

from __future__ import annotations

import logging
from pathlib import Path

from overtone.describe import DescriptionSpec, build_prompt
from overtone.gaps import Gap
from overtone.vision import VisionModel, describe

logger = logging.getLogger("overtone.author")


class VisionAuthor:
    """A callable that writes a fitted description for one gap.

    Instances are single-gap: construct one per gap with its frames and
    surrounding transcript, then hand it to the fit loop. It records each
    description it settles on so a caller can pass recent ones forward and
    avoid re-describing an unchanged slide in different words.
    """

    def __init__(
        self,
        gap: Gap,
        frames: list[Path],
        *,
        system_prompt: str,
        vision_chain: list[VisionModel],
        transcript_before: str = "",
        transcript_after: str = "",
        previous_descriptions: list[str] | None = None,
        max_tokens: int = 300,
    ):
        self._gap = gap
        self._frames = frames
        self._system = system_prompt
        self._chain = vision_chain
        self._before = transcript_before
        self._after = transcript_after
        self._previous = list(previous_descriptions or [])
        self._max_tokens = max_tokens
        self._attempt = 0
        self.last_text: str | None = None
        self.last_model: VisionModel | None = None

    @property
    def attempts(self) -> int:
        """How many times a description was written (one vision call each)."""
        return self._attempt

    def __call__(self, word_budget: int, feedback: str | None) -> str:
        self._attempt += 1
        spec = DescriptionSpec(
            gap=self._gap,
            word_budget=word_budget,
            transcript_before=self._before,
            transcript_after=self._after,
            previous_descriptions=self._previous,
            attempt=self._attempt,
            feedback=feedback,
        )
        prompt = build_prompt(spec)
        result = describe(
            self._frames,
            self._system,
            prompt,
            self._chain,
            max_tokens=self._max_tokens,
        )
        self.last_text = result.text
        self.last_model = result.model
        logger.debug(
            "gap %d attempt %d: %d-word budget -> %r via %s",
            self._gap.index,
            self._attempt,
            word_budget,
            result.text,
            result.model,
        )
        return result.text

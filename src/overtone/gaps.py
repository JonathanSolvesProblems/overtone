"""Dialogue-gap detection.

Audio description is spoken into the pauses that already exist in a video's
dialogue (WCAG 2.1 SC 1.2.5). Everything downstream depends on knowing exactly
where those pauses are and how long each one is, because a description that
overruns its pause talks over the next line of dialogue.

This module turns word-level timings from speech-to-text into a list of usable
gaps. It is deliberately free of any network or provider dependency so the
timing logic can be tested exhaustively without spending a cent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

# A pause shorter than this is not worth describing into. Professional
# describers treat roughly 1.5s as the floor for a useful phrase; below that
# there is only room for a word or two, which reads as noise.
DEFAULT_MIN_GAP = 1.5

# Leave a little silence on each side of a description so it never butts
# straight up against speech. Applied to both ends of every gap.
DEFAULT_EDGE_PAD = 0.15

# Narration pace used to convert an available number of seconds into a word
# budget. Audio description is usually delivered at 150-170 words per minute;
# 2.6 words/sec (156 wpm) is a deliberately conservative middle.
DEFAULT_WORDS_PER_SECOND = 2.6


class _Timed(Protocol):
    """Structural type for anything carrying a start and end in seconds.

    Matches ``genblaze_core.models.asset.WordTiming`` without importing it, so
    these helpers stay usable against plain test doubles.
    """

    start: float
    end: float


@dataclass(frozen=True)
class Gap:
    """A stretch of silence long enough to hold a spoken description.

    ``start`` and ``end`` are already padded, so they describe the window the
    narration may actually occupy, not the raw silence around it.
    """

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def word_budget(self, words_per_second: float = DEFAULT_WORDS_PER_SECOND) -> int:
        """How many words plausibly fit in this gap when spoken aloud.

        This is the budget handed to the description model. It is an estimate,
        not a guarantee: the real check happens after synthesis, when the
        rendered audio is measured against :attr:`duration`.
        """
        return max(1, int(self.duration * words_per_second))


def merge_speech_intervals(
    words: Iterable[_Timed],
    *,
    join_below: float = 0.0,
) -> list[tuple[float, float]]:
    """Collapse word timings into non-overlapping speech intervals.

    Speaker diarization routinely emits overlapping words when two people talk
    at once, and word lists are not guaranteed to arrive sorted. Merging first
    means gap detection sees one clean speech timeline rather than trying to
    reason about interleaved words.

    ``join_below`` additionally welds together intervals separated by less than
    that many seconds, which stops the natural micro-pauses between words from
    registering as gaps.
    """
    spans = sorted(
        ((float(w.start), float(w.end)) for w in words),
        key=lambda s: s[0],
    )
    if not spans:
        return []

    merged: list[tuple[float, float]] = []
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start - current_end <= join_below:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def find_gaps(
    words: Sequence[_Timed],
    *,
    media_duration: float,
    min_gap: float = DEFAULT_MIN_GAP,
    edge_pad: float = DEFAULT_EDGE_PAD,
) -> list[Gap]:
    """Find every pause in ``words`` long enough to describe into.

    Includes the lead-in before the first word and the tail after the last,
    which are often the roomiest gaps in a lecture recording and are easy to
    miss if you only look between words.

    Args:
        words: Word timings in seconds, in any order.
        media_duration: Total length of the video in seconds. Gaps are clamped
            to this so a transcript that overruns the file cannot produce a
            description pointing past the end.
        min_gap: Minimum usable pause length, measured after padding.
        edge_pad: Silence preserved at each end of a gap.

    Returns:
        Gaps in chronological order, renumbered from zero.
    """
    if media_duration <= 0:
        return []

    speech = merge_speech_intervals(words)

    # Complement of the speech timeline within [0, media_duration].
    raw: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in speech:
        if start > cursor:
            raw.append((cursor, min(start, media_duration)))
        cursor = max(cursor, end)
        if cursor >= media_duration:
            break
    if cursor < media_duration:
        raw.append((cursor, media_duration))

    gaps: list[Gap] = []
    for start, end in raw:
        padded_start = max(0.0, start + edge_pad)
        padded_end = min(media_duration, end - edge_pad)
        if padded_end - padded_start >= min_gap:
            gaps.append(Gap(index=len(gaps), start=padded_start, end=padded_end))
    return gaps


@dataclass(frozen=True)
class GapCoverage:
    """Summary of how describable a piece of media is.

    Used to warn on inputs where automated description cannot do an honest job.
    A dense lecture with no pauses will score low here, and saying so up front
    is more useful than silently emitting three descriptions for an hour of
    video.
    """

    media_duration: float
    speech_seconds: float
    gap_count: int
    describable_seconds: float

    @property
    def describable_ratio(self) -> float:
        if self.media_duration <= 0:
            return 0.0
        return self.describable_seconds / self.media_duration


def coverage(
    words: Sequence[_Timed],
    gaps: Sequence[Gap],
    *,
    media_duration: float,
) -> GapCoverage:
    """Summarize speech density and describable time for a piece of media."""
    speech = merge_speech_intervals(words)
    speech_seconds = sum(end - start for start, end in speech)
    return GapCoverage(
        media_duration=media_duration,
        speech_seconds=speech_seconds,
        gap_count=len(gaps),
        describable_seconds=sum(gap.duration for gap in gaps),
    )

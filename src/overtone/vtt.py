"""WebVTT output for descriptions.

Two flavours, both standards-grounded:

- A ``descriptions`` cue track (WebVTT ``kind=descriptions``), the machine-
  readable form a player or screen reader consumes to announce descriptions at
  the right time. This is the artifact that makes the output portable beyond
  the single mixed MP4.

- A plain text transcript of the descriptions, for a sighted reviewer or an
  accessibility auditor signing off on the track.

Timestamps follow the WebVTT ``HH:MM:SS.mmm`` grammar exactly, because players
are unforgiving about it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DescriptionCue:
    """One description placed on the timeline."""

    index: int
    start: float
    end: float
    text: str
    extended: bool = False  # True when the video pauses to fit this one (1.2.7)


def format_timestamp(seconds: float) -> str:
    """Render seconds as a WebVTT timestamp (``HH:MM:SS.mmm``)."""
    if seconds < 0:
        seconds = 0.0
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def build_vtt(cues: list[DescriptionCue], *, kind: str = "descriptions") -> str:
    """Build a WebVTT ``descriptions`` document from cues.

    The ``kind: descriptions`` header line is a WebVTT metadata header that
    marks the whole file as a description track, which is what a conforming
    player keys on to route these cues to a screen reader rather than the
    caption region.
    """
    lines = ["WEBVTT", f"kind: {kind}", ""]
    for cue in sorted(cues, key=lambda c: c.start):
        settings = " A:middle" if cue.extended else ""
        lines.append(f"{cue.index + 1}")
        lines.append(
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}{settings}"
        )
        # A note in the cue payload records where the video was frozen, so a
        # reviewer reading the raw VTT understands why the timing overlaps.
        if cue.extended:
            lines.append(f"<v Overtone (extended)>{cue.text}")
        else:
            lines.append(cue.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_transcript(cues: list[DescriptionCue]) -> str:
    """Build a human-readable description transcript for review sign-off."""
    lines = ["Audio description transcript", "=" * 28, ""]
    for cue in sorted(cues, key=lambda c: c.start):
        marker = " [extended: video paused]" if cue.extended else ""
        lines.append(f"[{format_timestamp(cue.start)}]{marker}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

"""Extended audio description (WCAG 2.1 SC 1.2.7).

Some moments simply cannot be described inside the pause that exists. A dense
diagram might appear during two seconds of silence and need eight seconds to
convey. The standards answer is not to talk faster or give up: it is to freeze
the picture, play the full description, then resume. The video gets longer, and
that is allowed, because the alternative is a blind viewer receiving less than
a sighted one.

This module inserts those freeze-frame segments. It splits the source at each
insertion point, builds a still clip that holds the frame for exactly as long
as the description takes to speak, and concatenates everything back in order.
Segments are normalized to one codec and timebase first, because the concat
demuxer will not join clips that disagree on either.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from overtone.ffmpeg import (
    DEFAULT_TIMEOUT,
    audio_duration,
    extract_frame,
    probe,
    resolve_binary,
    run,
)

# A breath of held frame after the narration ends, so the resume does not clip
# the last word.
FREEZE_TAIL = 0.25


@dataclass(frozen=True)
class FreezeInsertion:
    """A full description to play while the picture holds at ``at`` seconds."""

    at: float
    audio: Path

    def hold_seconds(self) -> float:
        return audio_duration(self.audio) + FREEZE_TAIL


def _normalize_segment(
    src: str | Path,
    dest: Path,
    *,
    start: float,
    end: float | None,
    fps: float,
    width: int,
    height: int,
    timeout: float,
) -> Path:
    """Re-encode a slice of the source to canonical params for concatenation."""
    ffmpeg = resolve_binary("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += [
        "-i",
        str(src),
        "-vf",
        f"scale={width}:{height},fps={fps},setsar=1",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        # A slice may fall in silence; guarantee an audio track exists so every
        # segment has the same stream layout for concat.
        "-af",
        "aresample=async=1:first_pts=0",
        "-y",
        str(dest),
    ]
    run(cmd, timeout=timeout)
    return dest


def _freeze_segment(
    src: str | Path,
    dest: Path,
    *,
    at: float,
    hold: float,
    audio: Path,
    fps: float,
    width: int,
    height: int,
    timeout: float,
) -> Path:
    """Build a still-frame clip: the frame at ``at`` held under ``audio``.

    The frame is grabbed to an image first, then looped. ``-loop`` is an image-
    demuxer option and does not apply to a video input, so freezing straight
    from the source fails; the extract-then-loop path is the portable one.
    """
    ffmpeg = resolve_binary("ffmpeg")
    still = dest.with_suffix(".png")
    extract_frame(src, at, still, width=width, timeout=timeout)
    run(
        [
            ffmpeg,
            "-nostdin",
            # Loop the grabbed still for the hold duration.
            "-loop",
            "1",
            "-i",
            str(still),
            "-i",
            str(audio),
            "-t",
            f"{hold:.3f}",
            "-vf",
            f"scale={width}:{height},fps={fps},setsar=1,format=yuv420p",
            # Pad the narration with trailing silence so the audio track spans
            # the full hold rather than ending early.
            "-af",
            "apad",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-y",
            str(dest),
        ],
        timeout=timeout,
    )
    still.unlink(missing_ok=True)
    return dest


def insert_freeze_descriptions(
    video: str | Path,
    insertions: list[FreezeInsertion],
    dest: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Insert freeze-frame descriptions into ``video`` at their timestamps.

    Returns the path to a new video, longer than the source by the total hold
    time. Insertions are applied in chronological order regardless of input
    order.
    """
    video = Path(video)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not insertions:
        raise ValueError("insert_freeze_descriptions requires at least one insertion")

    info = probe(video)
    fps = 30.0
    if info.has_video:
        # probe() does not surface fps; a fixed output rate keeps concat happy
        # and is imperceptible for a lecture. 30 is a safe default.
        fps = 30.0
    width = info.width or 1280
    height = info.height or 720
    # Even dimensions are required by yuv420p / libx264.
    width -= width % 2
    height -= height % 2

    ordered = sorted(insertions, key=lambda i: i.at)

    work = Path(tempfile.mkdtemp(prefix="overtone_ext_"))
    segments: list[Path] = []
    try:
        cursor = 0.0
        for idx, ins in enumerate(ordered):
            # The stretch of original video up to this insertion point.
            if ins.at > cursor:
                seg = _normalize_segment(
                    video,
                    work / f"orig_{idx}.mp4",
                    start=cursor,
                    end=ins.at,
                    fps=fps,
                    width=width,
                    height=height,
                    timeout=timeout,
                )
                segments.append(seg)

            frozen = _freeze_segment(
                video,
                work / f"freeze_{idx}.mp4",
                at=ins.at,
                hold=ins.hold_seconds(),
                audio=ins.audio,
                fps=fps,
                width=width,
                height=height,
                timeout=timeout,
            )
            segments.append(frozen)
            cursor = ins.at

        # The remaining tail after the last insertion.
        tail = _normalize_segment(
            video,
            work / "orig_tail.mp4",
            start=cursor,
            end=None,
            fps=fps,
            width=width,
            height=height,
            timeout=timeout,
        )
        segments.append(tail)

        concat_list = work / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{seg.as_posix()}'\n" for seg in segments),
            encoding="utf-8",
        )

        run(
            [
                resolve_binary("ffmpeg"),
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(dest),
            ],
            timeout=timeout,
        )
    finally:
        for seg in segments:
            seg.unlink(missing_ok=True)
        concat_txt = work / "concat.txt"
        concat_txt.unlink(missing_ok=True)
        work.rmdir()

    return dest

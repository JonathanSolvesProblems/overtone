"""Thin ffmpeg and ffprobe wrappers.

Genblaze ships equivalents of most of this in
``genblaze_core.providers._ffmpeg_utils``, but that module is private, so
depending on it would couple Overtone to an internal API. These are
deliberately small reimplementations. See the Genblaze feedback issue where we
ask for them to be exported.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 600


class FFmpegError(RuntimeError):
    """An ffmpeg or ffprobe invocation failed."""


def resolve_binary(name: str) -> str:
    """Locate an executable, with an actionable error if it is missing."""
    found = shutil.which(name)
    if found is None:
        raise FFmpegError(
            f"{name} not found on PATH. Install ffmpeg: https://ffmpeg.org/download.html"
        )
    return found


def run(cmd: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    """Run a command, raising :class:`FFmpegError` with stderr on failure."""
    try:
        # check=False on purpose: the non-zero path is handled below, where
        # ffmpeg's stderr is surfaced in the raised message.
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{cmd[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise FFmpegError(f"Failed to run {cmd[0]}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        tail = stderr[-800:] if stderr else "(no stderr)"
        raise FFmpegError(f"{Path(cmd[0]).name} exited {result.returncode}: {tail}")
    return result


@dataclass(frozen=True)
class MediaInfo:
    """The handful of facts about a media file that the pipeline cares about."""

    duration: float
    has_audio: bool
    has_video: bool
    width: int | None = None
    height: int | None = None


def probe(path: str | Path, *, timeout: float = 60) -> MediaInfo:
    """Read duration and stream layout via ffprobe."""
    ffprobe = resolve_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    payload = json.loads(run(cmd, timeout=timeout).stdout.decode(errors="replace"))

    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = payload.get("format", {}).get("duration")
    if duration is None and video is not None:
        duration = video.get("duration")

    return MediaInfo(
        duration=float(duration) if duration is not None else 0.0,
        has_audio=audio is not None,
        has_video=video is not None,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
    )


def audio_duration(path: str | Path) -> float:
    """Duration of an audio file in seconds.

    This is the measurement the fit loop turns on: it is the ground truth for
    whether a synthesized description actually fits its gap, as opposed to the
    word-count estimate used to write it.
    """
    return probe(path).duration


def extract_audio(
    video: str | Path,
    dest: str | Path,
    *,
    sample_rate: int = 16000,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Extract a mono audio track suitable for speech-to-text.

    16 kHz mono is what transcription models want, and it keeps the upload to
    the STT provider small: a 50 minute lecture becomes about 90 MB of WAV
    rather than several GB of video.
    """
    ffmpeg = resolve_binary("ffmpeg")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg,
            "-nostdin",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(dest),
        ],
        timeout=timeout,
    )
    return dest


def extract_frame(
    video: str | Path,
    at: float,
    dest: str | Path,
    *,
    width: int = 768,
    timeout: float = 120,
) -> Path:
    """Grab a single frame at ``at`` seconds, downscaled for a vision model.

    Seeking before ``-i`` is the fast path: ffmpeg jumps to the nearest
    keyframe instead of decoding from the start, which matters when pulling
    dozens of frames from an hour-long file.
    """
    ffmpeg = resolve_binary("ffmpeg")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg,
            "-nostdin",
            "-ss",
            f"{max(0.0, at):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            "-y",
            str(dest),
        ],
        timeout=timeout,
    )
    return dest


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Return ``(hex_digest, size_bytes)`` for a file, streamed."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size

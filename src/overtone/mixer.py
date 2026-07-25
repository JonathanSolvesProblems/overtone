"""Timeline-accurate mixing of description clips into a program's audio.

Genblaze ships an ``FFmpegCompositor``, but it muxes one whole audio track onto
one whole video track. Audio description needs something different: a dozen or
more short clips dropped into precise offsets of the existing audio, with the
program dipped underneath each one so the narration stays intelligible over
music and room tone.

The filter graph is written to a script file rather than passed on the command
line, because an hour-long lecture can carry eighty descriptions and the
resulting graph comfortably exceeds the Windows command-line limit.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from overtone.ffmpeg import DEFAULT_TIMEOUT, probe, resolve_binary, run

# How far the program audio is dipped while a description plays. Descriptions
# are placed in dialogue pauses, but a pause in speech is rarely a pause in
# music or ambience, so some ducking is still needed. 0.35 is roughly -9 dB.
DEFAULT_DUCK_GAIN = 0.35

# Everything is resampled to this before mixing. Description clips come back
# from different TTS vendors at different rates and layouts, and amix refuses
# to combine mismatched inputs.
MIX_SAMPLE_RATE = 48000
MIX_LAYOUT = "stereo"

_AFORMAT = f"aformat=sample_fmts=fltp:sample_rates={MIX_SAMPLE_RATE}:channel_layouts={MIX_LAYOUT}"


@dataclass(frozen=True)
class Placement:
    """One description clip and the moment it starts playing."""

    at: float
    audio: Path
    duration: float | None = None

    @property
    def end(self) -> float | None:
        return None if self.duration is None else self.at + self.duration


def build_filter_graph(
    placements: list[Placement],
    *,
    base_has_audio: bool,
    duck_gain: float = DEFAULT_DUCK_GAIN,
) -> str:
    """Build the ffmpeg filter graph that lays descriptions over the program.

    Input 0 is the program. Description clips are inputs 1..n, in the same
    order as ``placements``. When the program has no audio track of its own,
    input n+1 is a generated silent bed so there is always something to mix
    against.
    """
    if not placements:
        raise ValueError("build_filter_graph requires at least one placement")

    base_label = "0:a" if base_has_audio else f"{len(placements) + 1}:a"

    chains: list[str] = []

    # Dip the program under every description window. Terms are summed, which
    # ffmpeg reads as a boolean OR: any non-zero result enables the filter.
    duck_windows = [p for p in placements if p.end is not None]
    if duck_windows and duck_gain < 1.0:
        condition = "+".join(f"between(t,{p.at:.3f},{p.end:.3f})" for p in duck_windows)
        chains.append(f"[{base_label}]{_AFORMAT},volume=enable='{condition}':volume={duck_gain}[base]")
    else:
        chains.append(f"[{base_label}]{_AFORMAT}[base]")

    for index, placement in enumerate(placements):
        delay_ms = round(max(0.0, placement.at) * 1000)
        chains.append(f"[{index + 1}:a]{_AFORMAT},adelay=delays={delay_ms}:all=1[d{index}]")

    mix_inputs = "[base]" + "".join(f"[d{i}]" for i in range(len(placements)))
    chains.append(
        f"{mix_inputs}amix=inputs={len(placements) + 1}:duration=first:normalize=0[aout]"
    )
    return ";".join(chains)


def mix_descriptions(
    video: str | Path,
    placements: list[Placement],
    dest: str | Path,
    *,
    duck_gain: float = DEFAULT_DUCK_GAIN,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Render ``video`` with description clips mixed into its audio.

    The video stream is stream-copied, so this re-encodes audio only. A 50
    minute lecture mixes in seconds rather than minutes, and the picture is
    bit-identical to the source.
    """
    video = Path(video)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not placements:
        raise ValueError("mix_descriptions requires at least one placement")

    info = probe(video)
    ffmpeg = resolve_binary("ffmpeg")

    cmd: list[str] = [ffmpeg, "-nostdin", "-i", str(video)]
    for placement in placements:
        cmd += ["-i", str(placement.audio)]
    if not info.has_audio:
        # A silent bed the length of the picture, so offsets stay absolute.
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{info.duration:.3f}",
            "-i",
            f"anullsrc=channel_layout={MIX_LAYOUT}:sample_rate={MIX_SAMPLE_RATE}",
        ]

    graph = build_filter_graph(
        placements,
        base_has_audio=info.has_audio,
        duck_gain=duck_gain,
    )

    # mkstemp hands back an open descriptor. Close it before writing, or
    # Windows refuses to unlink the file afterwards.
    handle, script_path = tempfile.mkstemp(suffix=".ffgraph", text=True)
    os.close(handle)
    script = Path(script_path)
    script.write_text(graph, encoding="utf-8")
    try:
        cmd += [
            "-filter_complex_script",
            str(script),
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            str(dest),
        ]
        run(cmd, timeout=timeout)
    finally:
        script.unlink(missing_ok=True)

    return dest

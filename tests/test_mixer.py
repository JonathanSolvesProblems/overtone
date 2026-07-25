"""Mixing tests.

The graph-building tests are pure string assertions. The render test actually
shells out to ffmpeg against synthetic media, which proves the whole muxing
path without touching a paid provider.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from overtone.ffmpeg import probe, resolve_binary, run
from overtone.mixer import Placement, build_filter_graph, mix_descriptions

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class TestFilterGraph:
    def test_rejects_empty_placements(self):
        with pytest.raises(ValueError):
            build_filter_graph([], base_has_audio=True)

    def test_delays_are_milliseconds(self):
        graph = build_filter_graph(
            [Placement(at=12.4, audio=Path("d.wav"), duration=2.0)],
            base_has_audio=True,
        )
        assert "adelay=delays=12400:all=1" in graph

    def test_program_audio_is_input_zero_when_present(self):
        graph = build_filter_graph(
            [Placement(at=1.0, audio=Path("d.wav"), duration=1.0)],
            base_has_audio=True,
        )
        assert graph.startswith("[0:a]")

    def test_silent_program_gets_a_generated_bed(self):
        # With one description, the null source is input 2.
        graph = build_filter_graph(
            [Placement(at=1.0, audio=Path("d.wav"), duration=1.0)],
            base_has_audio=False,
        )
        assert graph.startswith("[2:a]")

    def test_ducking_covers_every_described_window(self):
        graph = build_filter_graph(
            [
                Placement(at=1.0, audio=Path("a.wav"), duration=2.0),
                Placement(at=10.0, audio=Path("b.wav"), duration=1.5),
            ],
            base_has_audio=True,
            duck_gain=0.4,
        )
        assert "between(t,1.000,3.000)+between(t,10.000,11.500)" in graph
        assert "volume=0.4" in graph

    def test_ducking_is_skipped_when_durations_are_unknown(self):
        graph = build_filter_graph(
            [Placement(at=1.0, audio=Path("a.wav"))],
            base_has_audio=True,
        )
        assert "volume=enable" not in graph

    def test_ducking_can_be_disabled(self):
        graph = build_filter_graph(
            [Placement(at=1.0, audio=Path("a.wav"), duration=2.0)],
            base_has_audio=True,
            duck_gain=1.0,
        )
        assert "volume=enable" not in graph

    def test_mix_input_count_includes_the_base(self):
        graph = build_filter_graph(
            [
                Placement(at=1.0, audio=Path("a.wav"), duration=1.0),
                Placement(at=5.0, audio=Path("b.wav"), duration=1.0),
                Placement(at=9.0, audio=Path("c.wav"), duration=1.0),
            ],
            base_has_audio=True,
        )
        assert "amix=inputs=4:duration=first:normalize=0" in graph
        assert "[base][d0][d1][d2]amix" in graph


def _make_video(dest: Path, *, seconds: float, silent: bool = False) -> Path:
    """Synthesize a test video, optionally with no audio track."""
    ffmpeg = resolve_binary("ffmpeg")
    cmd = [
        ffmpeg,
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=320x240:rate=15:duration={seconds}",
    ]
    if not silent:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds)]
    if not silent:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += ["-y", str(dest)]
    run(cmd)
    return dest


def _make_tone(dest: Path, *, seconds: float, hz: int = 880) -> Path:
    ffmpeg = resolve_binary("ffmpeg")
    run(
        [
            ffmpeg,
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={hz}:duration={seconds}",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(dest),
        ]
    )
    return dest


@needs_ffmpeg
def test_probe_reads_duration_and_streams(tmp_path):
    video = _make_video(tmp_path / "v.mp4", seconds=4)
    info = probe(video)
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(4.0, abs=0.3)
    assert (info.width, info.height) == (320, 240)


@needs_ffmpeg
def test_probe_detects_a_silent_video(tmp_path):
    video = _make_video(tmp_path / "silent.mp4", seconds=3, silent=True)
    assert probe(video).has_audio is False


@needs_ffmpeg
def test_mix_preserves_duration_and_adds_audio(tmp_path):
    video = _make_video(tmp_path / "v.mp4", seconds=20)
    placements = [
        Placement(at=6.0, audio=_make_tone(tmp_path / "d0.wav", seconds=2.0), duration=2.0),
        Placement(at=12.5, audio=_make_tone(tmp_path / "d1.wav", seconds=1.5), duration=1.5),
    ]
    out = mix_descriptions(video, placements, tmp_path / "out.mp4")

    info = probe(out)
    assert info.has_video and info.has_audio
    # duration=first keeps the program's length; descriptions never extend it.
    assert info.duration == pytest.approx(20.0, abs=0.5)


@needs_ffmpeg
def test_mix_works_on_a_silent_program(tmp_path):
    video = _make_video(tmp_path / "silent.mp4", seconds=10, silent=True)
    placements = [
        Placement(at=2.0, audio=_make_tone(tmp_path / "d.wav", seconds=1.5), duration=1.5)
    ]
    out = mix_descriptions(video, placements, tmp_path / "out.mp4")

    info = probe(out)
    assert info.has_audio is True
    assert info.duration == pytest.approx(10.0, abs=0.5)


@needs_ffmpeg
def test_description_near_the_end_does_not_extend_the_program(tmp_path):
    video = _make_video(tmp_path / "v.mp4", seconds=8)
    placements = [
        Placement(at=7.5, audio=_make_tone(tmp_path / "d.wav", seconds=3.0), duration=3.0)
    ]
    out = mix_descriptions(video, placements, tmp_path / "out.mp4")
    assert probe(out).duration == pytest.approx(8.0, abs=0.5)

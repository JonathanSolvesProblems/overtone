"""Extended-description (freeze-frame) tests.

These render real video with ffmpeg, so they are the slow ones. They verify the
core promise of the 1.2.7 path: the output is longer than the source by the
hold time, and it still plays.
"""

from __future__ import annotations

import shutil

import pytest

from overtone.extended import FREEZE_TAIL, FreezeInsertion, insert_freeze_descriptions
from overtone.ffmpeg import probe, resolve_binary, run

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _video(dest, *, seconds: float):
    run(
        [
            resolve_binary("ffmpeg"),
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=200:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(dest),
        ]
    )
    return dest


def _tone(dest, *, seconds: float):
    run(
        [
            resolve_binary("ffmpeg"),
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(dest),
        ]
    )
    return dest


def test_requires_at_least_one_insertion(tmp_path):
    with pytest.raises(ValueError):
        insert_freeze_descriptions(tmp_path / "x.mp4", [], tmp_path / "out.mp4")


@needs_ffmpeg
def test_hold_seconds_includes_the_tail(tmp_path):
    audio = _tone(tmp_path / "a.wav", seconds=3.0)
    ins = FreezeInsertion(at=5.0, audio=audio)
    assert ins.hold_seconds() == pytest.approx(3.0 + FREEZE_TAIL, abs=0.15)


@needs_ffmpeg
def test_output_is_longer_by_the_hold_time(tmp_path):
    video = _video(tmp_path / "v.mp4", seconds=10)
    audio = _tone(tmp_path / "d.wav", seconds=4.0)
    ins = FreezeInsertion(at=5.0, audio=audio)

    out = insert_freeze_descriptions(video, [ins], tmp_path / "out.mp4")

    info = probe(out)
    expected = 10 + ins.hold_seconds()
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(expected, abs=1.0)


@needs_ffmpeg
def test_multiple_insertions_apply_in_time_order(tmp_path):
    video = _video(tmp_path / "v.mp4", seconds=12)
    a1 = _tone(tmp_path / "d1.wav", seconds=2.0)
    a2 = _tone(tmp_path / "d2.wav", seconds=3.0)
    # Deliberately out of order to prove sorting.
    insertions = [
        FreezeInsertion(at=8.0, audio=a2),
        FreezeInsertion(at=3.0, audio=a1),
    ]

    out = insert_freeze_descriptions(video, insertions, tmp_path / "out.mp4")

    info = probe(out)
    expected = 12 + insertions[0].hold_seconds() + insertions[1].hold_seconds()
    assert info.duration == pytest.approx(expected, abs=1.5)


@needs_ffmpeg
def test_insertion_at_the_very_start(tmp_path):
    video = _video(tmp_path / "v.mp4", seconds=6)
    audio = _tone(tmp_path / "d.wav", seconds=2.0)
    ins = FreezeInsertion(at=0.0, audio=audio)

    out = insert_freeze_descriptions(video, [ins], tmp_path / "out.mp4")
    assert probe(out).duration == pytest.approx(6 + ins.hold_seconds(), abs=1.0)

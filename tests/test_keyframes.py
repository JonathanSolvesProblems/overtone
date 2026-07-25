"""Frame sampling and visual-change tests."""

from __future__ import annotations

import shutil

import pytest

from overtone.ffmpeg import resolve_binary, run
from overtone.gaps import Gap
from overtone.keyframes import (
    FrameSignature,
    VisualChangeTracker,
    average_hash,
    collect,
    colour_distance,
    dhash,
    hamming,
    mean_rgb,
    sample_times,
    signature,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class TestSampleTimes:
    def test_returns_requested_count_in_order(self):
        times = sample_times(Gap(0, 10.0, 14.0), count=3)
        assert len(times) == 3
        assert times == sorted(times)

    def test_skips_the_opening_of_the_gap(self):
        # The first frame must not sit at the very start, where a slide may
        # still be mid-transition. 4s gap -> lead of min(0.5, 1.2) = 0.5.
        first = sample_times(Gap(0, 10.0, 14.0), count=3)[0]
        assert first == pytest.approx(10.5)

    def test_lead_is_capped_for_short_gaps(self):
        # 1s gap -> lead of 0.3s, not 0.5.
        first = sample_times(Gap(0, 10.0, 11.0), count=3)[0]
        assert first == pytest.approx(10.3)

    def test_reaches_past_the_gap_to_catch_the_next_slide(self):
        times = sample_times(Gap(0, 10.0, 14.0), count=3, lookahead=0.4)
        assert times[-1] == pytest.approx(14.4)

    def test_single_frame_uses_the_settled_middle(self):
        # Not the very start: 60% into a 4s gap from 5.0 -> 7.4.
        assert sample_times(Gap(0, 5.0, 9.0), count=1) == [pytest.approx(7.4)]


class TestHashing:
    def test_hamming_of_identical_hashes_is_zero(self):
        assert hamming(0b1011, 0b1011) == 0

    def test_hamming_counts_differing_bits(self):
        assert hamming(0b1111, 0b1010) == 2


def sig(bits: int, colour: tuple[int, int, int] = (128, 128, 128)) -> FrameSignature:
    return FrameSignature(bits=bits, colour=colour)


class TestVisualChangeTracker:
    def test_first_frame_is_always_new(self):
        assert VisualChangeTracker().is_new(sig(12345)) is True

    def test_identical_frame_is_not_new(self):
        tracker = VisualChangeTracker(threshold=6)
        tracker.accept(sig(0b1010))
        assert tracker.is_new(sig(0b1010)) is False

    def test_small_difference_is_not_new(self):
        # One bit of compression noise must not read as a slide change.
        tracker = VisualChangeTracker(threshold=6)
        tracker.accept(sig(0))
        assert tracker.is_new(sig(0b111)) is False

    def test_large_structural_difference_is_new(self):
        tracker = VisualChangeTracker(threshold=6)
        tracker.accept(sig(0))
        assert tracker.is_new(sig(0xFFFFFFFF)) is True

    def test_colour_change_alone_is_new(self):
        # Same layout, different colour. A luminance-only hash misses this
        # entirely, which is what the flat-frame case exposed.
        tracker = VisualChangeTracker(threshold=6, colour_threshold=18)
        tracker.accept(sig(0, colour=(200, 30, 30)))
        assert tracker.is_new(sig(0, colour=(30, 30, 200))) is True

    def test_slight_exposure_drift_is_not_new(self):
        tracker = VisualChangeTracker(threshold=6, colour_threshold=18)
        tracker.accept(sig(0, colour=(120, 120, 120)))
        assert tracker.is_new(sig(0, colour=(130, 128, 126))) is False

    def test_only_accepted_frames_become_the_baseline(self):
        # A skipped near-duplicate must not drag the baseline along with it.
        tracker = VisualChangeTracker(threshold=6)
        baseline = sig(0)
        tracker.accept(baseline)
        tracker.is_new(sig(0b111))
        assert tracker.last_described == baseline


def _two_scene_video(dest, *, seconds_each: float = 4):
    ffmpeg = resolve_binary("ffmpeg")
    run(
        [
            ffmpeg,
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s=320x240:r=10:d={seconds_each}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=320x240:r=10:d={seconds_each}",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(dest),
        ]
    )
    return dest


@needs_ffmpeg
def test_identical_frames_hash_the_same(tmp_path):
    from overtone.ffmpeg import extract_frame

    video = _two_scene_video(tmp_path / "v.mp4")
    a = extract_frame(video, 1.0, tmp_path / "a.jpg")
    b = extract_frame(video, 2.0, tmp_path / "b.jpg")
    assert hamming(average_hash(a), average_hash(b)) == 0


def _text_slide(dest, text, *, size: int = 24, fontcolor="white"):
    from overtone.ffmpeg import resolve_binary, run

    font = "C\\:/Windows/Fonts/arial.ttf"
    run(
        [
            resolve_binary("ffmpeg"),
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x0b1020:s=640x360:d=1",
            "-vf",
            (
                f"drawtext=fontfile='{font}':text='{text}':fontcolor={fontcolor}:"
                f"fontsize={size}:x=(w-tw)/2:y=(h-th)/2"
            ),
            "-frames:v",
            "1",
            "-y",
            str(dest),
        ]
    )
    return dest


@needs_ffmpeg
def test_dhash_distinguishes_two_text_slides(tmp_path):
    # The failure mode a mean hash has on lectures: two slides that differ only
    # in a line of centred text on a dark background. dHash must keep them apart
    # where average hash collapses them.
    a = _text_slide(tmp_path / "a.png", "y = m x + b")
    b = _text_slide(tmp_path / "b.png", "slope m = rise / run")

    avg = hamming(average_hash(a, size=16), average_hash(b, size=16))
    dh = hamming(dhash(a), dhash(b))
    assert dh > avg
    assert dh >= 12  # comfortably above the similarity threshold

    # And an identical slide re-rendered stays near zero.
    a2 = _text_slide(tmp_path / "a2.png", "y = m x + b")
    assert hamming(dhash(a), dhash(a2)) <= 6


@needs_ffmpeg
def test_collect_flags_the_repeated_slide_as_not_new(tmp_path):
    video = _two_scene_video(tmp_path / "v.mp4")
    gaps = [
        Gap(0, 0.5, 2.0),   # red
        Gap(1, 2.2, 3.5),   # still red, nothing new to say
        Gap(2, 4.5, 6.5),   # blue, a genuine change
    ]
    frames = collect(video, gaps, tmp_path / "frames", count=2)

    assert [f.is_new for f in frames] == [True, False, True]


@needs_ffmpeg
def test_flat_frames_are_distinguished_by_colour_not_layout(tmp_path):
    from overtone.ffmpeg import extract_frame

    video = _two_scene_video(tmp_path / "v.mp4")
    red = signature(extract_frame(video, 1.0, tmp_path / "red.jpg"))
    blue = signature(extract_frame(video, 6.0, tmp_path / "blue.jpg"))

    # Both frames are flat, so every pixel sits at the mean and the structural
    # hashes collide. Only the colour signature separates them.
    assert hamming(red.bits, blue.bits) == 0
    assert colour_distance(red, blue) > 18


@needs_ffmpeg
def test_mean_rgb_reads_the_dominant_colour(tmp_path):
    from overtone.ffmpeg import extract_frame

    video = _two_scene_video(tmp_path / "v.mp4")
    r, g, b = mean_rgb(extract_frame(video, 1.0, tmp_path / "red.jpg"))
    assert r > g and r > b


@needs_ffmpeg
def test_collect_writes_one_file_per_sampled_time(tmp_path):
    video = _two_scene_video(tmp_path / "v.mp4")
    frames = collect(video, [Gap(0, 0.5, 2.5)], tmp_path / "frames", count=3)
    assert len(frames[0].paths) == 3
    assert all(p.exists() and p.stat().st_size > 0 for p in frames[0].paths)

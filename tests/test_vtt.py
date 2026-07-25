"""WebVTT formatting tests (pure, no ffmpeg)."""

from __future__ import annotations

from overtone.vtt import (
    DescriptionCue,
    build_transcript,
    build_vtt,
    format_timestamp,
)


class TestTimestamp:
    def test_zero(self):
        assert format_timestamp(0) == "00:00:00.000"

    def test_milliseconds_are_rounded(self):
        assert format_timestamp(1.2345) == "00:00:01.234"
        assert format_timestamp(1.2346) == "00:00:01.235"

    def test_minutes_and_hours(self):
        assert format_timestamp(3661.5) == "01:01:01.500"

    def test_negative_clamps_to_zero(self):
        assert format_timestamp(-3) == "00:00:00.000"

    def test_exact_minute(self):
        assert format_timestamp(60) == "00:01:00.000"


class TestBuildVtt:
    def test_starts_with_the_webvtt_header(self):
        vtt = build_vtt([DescriptionCue(0, 1.0, 3.0, "A red slide.")])
        assert vtt.startswith("WEBVTT")

    def test_marks_the_track_as_descriptions(self):
        vtt = build_vtt([DescriptionCue(0, 1.0, 3.0, "A red slide.")])
        assert "kind: descriptions" in vtt

    def test_cue_has_a_timing_line(self):
        vtt = build_vtt([DescriptionCue(0, 1.0, 3.5, "A red slide.")])
        assert "00:00:01.000 --> 00:00:03.500" in vtt
        assert "A red slide." in vtt

    def test_cues_are_sorted_by_start(self):
        cues = [
            DescriptionCue(1, 10.0, 12.0, "second"),
            DescriptionCue(0, 1.0, 3.0, "first"),
        ]
        vtt = build_vtt(cues)
        assert vtt.index("first") < vtt.index("second")

    def test_cue_numbers_are_one_based(self):
        vtt = build_vtt([DescriptionCue(0, 1.0, 3.0, "x")])
        # The cue identifier line is the index+1.
        assert "\n1\n" in "\n" + vtt

    def test_extended_cue_is_annotated(self):
        vtt = build_vtt([DescriptionCue(0, 5.0, 13.0, "A dense diagram.", extended=True)])
        assert "Overtone (extended)" in vtt

    def test_trailing_newline(self):
        vtt = build_vtt([DescriptionCue(0, 1.0, 3.0, "x")])
        assert vtt.endswith("\n")


class TestTranscript:
    def test_lists_each_description_with_a_timestamp(self):
        cues = [
            DescriptionCue(0, 1.0, 3.0, "A title card."),
            DescriptionCue(1, 8.0, 10.0, "A scatter plot."),
        ]
        text = build_transcript(cues)
        assert "[00:00:01.000]" in text
        assert "A title card." in text
        assert "A scatter plot." in text

    def test_flags_extended_descriptions(self):
        text = build_transcript([DescriptionCue(0, 5.0, 13.0, "A diagram.", extended=True)])
        assert "video paused" in text

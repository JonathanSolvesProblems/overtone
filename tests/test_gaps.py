"""Timing logic tests.

These run without API keys or network. If the gap map is wrong, every
description lands in the wrong place, so this is the part worth over-testing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from overtone.gaps import (
    DEFAULT_EDGE_PAD,
    Gap,
    coverage,
    find_gaps,
    merge_speech_intervals,
)


@dataclass
class W:
    """Minimal stand-in for genblaze's WordTiming."""

    start: float
    end: float
    word: str = "x"


def test_no_words_yields_whole_media_as_one_gap():
    gaps = find_gaps([], media_duration=10.0)
    assert len(gaps) == 1
    assert gaps[0].start == pytest.approx(DEFAULT_EDGE_PAD)
    assert gaps[0].end == pytest.approx(10.0 - DEFAULT_EDGE_PAD)


def test_zero_duration_media_yields_nothing():
    assert find_gaps([W(0, 1)], media_duration=0.0) == []


def test_finds_gap_between_two_utterances():
    words = [W(0.0, 2.0), W(6.0, 8.0)]
    gaps = find_gaps(words, media_duration=8.0, edge_pad=0.0)
    assert len(gaps) == 1
    assert gaps[0].start == pytest.approx(2.0)
    assert gaps[0].end == pytest.approx(6.0)


def test_includes_lead_in_and_tail():
    words = [W(3.0, 5.0)]
    gaps = find_gaps(words, media_duration=12.0, edge_pad=0.0)
    assert [(g.start, g.end) for g in gaps] == [(0.0, 3.0), (5.0, 12.0)]


def test_short_pauses_are_rejected():
    # 0.8s of silence is real but too short to describe into.
    words = [W(0.0, 2.0), W(2.8, 5.0)]
    assert find_gaps(words, media_duration=5.0, edge_pad=0.0) == []


def test_min_gap_is_measured_after_padding():
    # 1.6s raw silence, but 0.15s padding each side leaves 1.3s, under the
    # 1.5s floor. It must not survive.
    words = [W(0.0, 2.0), W(3.6, 5.0)]
    gaps = find_gaps(words, media_duration=5.0, min_gap=1.5, edge_pad=0.15)
    assert gaps == []


def test_padding_shrinks_the_usable_window():
    words = [W(0.0, 2.0), W(8.0, 10.0)]
    gaps = find_gaps(words, media_duration=10.0, edge_pad=0.25)
    assert gaps[0].start == pytest.approx(2.25)
    assert gaps[0].end == pytest.approx(7.75)


def test_unsorted_words_are_handled():
    words = [W(6.0, 8.0), W(0.0, 2.0)]
    gaps = find_gaps(words, media_duration=8.0, edge_pad=0.0)
    assert [(g.start, g.end) for g in gaps] == [(2.0, 6.0)]


def test_overlapping_speakers_do_not_invent_a_gap():
    # Two speakers talking over each other. The naive "end of word N to start
    # of word N+1" reading would see a 1.9s gap here that does not exist.
    words = [W(0.0, 5.0), W(3.1, 4.0), W(5.2, 9.0)]
    gaps = find_gaps(words, media_duration=9.0, edge_pad=0.0)
    assert gaps == []


def test_transcript_overrunning_media_is_clamped():
    words = [W(0.0, 2.0), W(30.0, 40.0)]
    gaps = find_gaps(words, media_duration=10.0, edge_pad=0.0)
    assert len(gaps) == 1
    assert gaps[0].end == pytest.approx(10.0)


def test_gaps_are_renumbered_in_order():
    words = [W(2.0, 3.0), W(9.0, 10.0)]
    gaps = find_gaps(words, media_duration=20.0, edge_pad=0.0)
    assert [g.index for g in gaps] == list(range(len(gaps)))
    assert gaps == sorted(gaps, key=lambda g: g.start)


def test_merge_joins_micro_pauses_when_asked():
    words = [W(0.0, 1.0), W(1.05, 2.0)]
    assert merge_speech_intervals(words, join_below=0.1) == [(0.0, 2.0)]
    assert merge_speech_intervals(words, join_below=0.0) == [(0.0, 1.0), (1.05, 2.0)]


class TestWordBudget:
    def test_scales_with_duration(self):
        assert Gap(0, 0.0, 10.0).word_budget(words_per_second=2.0) == 20

    def test_never_returns_zero(self):
        # A gap that scrapes past the floor still gets to say something.
        assert Gap(0, 0.0, 0.1).word_budget() == 1


def test_coverage_reports_describable_ratio():
    words = [W(0.0, 5.0), W(15.0, 20.0)]
    gaps = find_gaps(words, media_duration=20.0, edge_pad=0.0)
    cov = coverage(words, gaps, media_duration=20.0)
    assert cov.speech_seconds == pytest.approx(10.0)
    assert cov.gap_count == 1
    assert cov.describable_seconds == pytest.approx(10.0)
    assert cov.describable_ratio == pytest.approx(0.5)


def test_coverage_of_wall_to_wall_speech_is_zero():
    words = [W(0.0, 30.0)]
    gaps = find_gaps(words, media_duration=30.0)
    cov = coverage(words, gaps, media_duration=30.0)
    assert cov.gap_count == 0
    assert cov.describable_ratio == 0.0

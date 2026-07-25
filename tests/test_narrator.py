"""Fit-loop tests, run entirely against a mock TTS provider.

The mock synthesizes a real (silent) audio file whose length is proportional to
the word count, at a fixed speaking rate. That makes ``audio_duration`` return a
true measurement while costing nothing, so the loop's convergence is proven for
real rather than asserted.
"""

from __future__ import annotations

import shutil

import pytest
from genblaze_core._utils import local_file_url
from genblaze_core.mocks import MockProvider
from genblaze_core.models.asset import Asset, AudioMetadata

from overtone.ffmpeg import resolve_binary, run
from overtone.gaps import Gap
from overtone.narrator import fit_description, make_fit_loop

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class SpeakingMock(MockProvider):
    """A TTS mock that renders silence as long as the text takes to speak.

    ``rate`` is words per second. Each generate() writes a wav of
    ``word_count / rate`` seconds, so the fit loop sees genuine durations and
    must actually shorten the text to converge.
    """

    def __init__(self, tmp_path, *, rate: float = 3.0):
        self._tmp = tmp_path
        self._rate = rate
        self._counter = 0
        super().__init__(name="mock-tts", assets=self._render)

    def _render(self, step):
        words = len((step.prompt or "").split())
        seconds = max(0.1, words / self._rate)
        self._counter += 1
        dest = self._tmp / f"clip_{self._counter}.wav"
        run(
            [
                resolve_binary("ffmpeg"),
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                f"{seconds:.3f}",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(dest),
            ]
        )
        asset = Asset(url=local_file_url(dest.resolve()), media_type="audio/wav")
        asset.audio = AudioMetadata(codec="pcm", channels=1, sample_rate=44100)
        asset.duration = seconds
        return [asset]


def make_describe_fn(rate_words_per_sentence=None):
    """Return a describe_fn that respects the requested word budget.

    Simulates a well-behaved author: on the first call it writes an overlong
    description; when asked for fewer words it complies, so the loop can reach a
    fit.
    """
    state = {"calls": 0}

    def describe_fn(budget: int, feedback):
        state["calls"] += 1
        if feedback is None:
            # First pass: deliberately verbose, 20 words regardless of budget,
            # to force at least one retry on a short gap.
            return " ".join(["word"] * 20)
        # Retry: honor the tightened budget.
        return " ".join(["word"] * budget)

    describe_fn.state = state
    return describe_fn


@needs_ffmpeg
def test_short_gap_forces_a_retry_and_converges(tmp_path):
    # 3s gap. At 3 words/sec that holds ~9 words, but the first draft is 20.
    gap = Gap(0, 0.0, 3.0)
    tts = SpeakingMock(tmp_path, rate=3.0)
    describe_fn = make_describe_fn()

    result = fit_description(gap, describe_fn, tts, model="mock", max_iterations=4)

    assert result.fits is True
    assert result.attempts >= 2  # the 20-word draft cannot have fit
    assert result.spoken_seconds <= gap.duration
    assert result.audio_path.exists()


@needs_ffmpeg
def test_generous_gap_fits_on_first_try(tmp_path):
    # 30s gap easily holds a 20-word draft; no retry needed.
    gap = Gap(0, 0.0, 30.0)
    tts = SpeakingMock(tmp_path, rate=3.0)
    describe_fn = make_describe_fn()

    result = fit_description(gap, describe_fn, tts, model="mock", max_iterations=4)

    assert result.fits is True
    assert result.attempts == 1


@needs_ffmpeg
def test_gives_up_after_max_iterations_but_returns_best(tmp_path):
    # An author that ignores the budget and always writes 20 words can never
    # fit a 2s gap. The loop must stop and still hand back a usable result.
    gap = Gap(0, 0.0, 2.0)
    tts = SpeakingMock(tmp_path, rate=3.0)

    def stubborn(budget, feedback):
        return " ".join(["word"] * 20)

    result = fit_description(gap, stubborn, tts, model="mock", max_iterations=3)

    assert result.fits is False
    assert result.attempts == 3
    assert result.audio_path.exists()  # best (last) attempt still rendered


@needs_ffmpeg
def test_feedback_tightens_the_budget_across_iterations(tmp_path):
    gap = Gap(0, 0.0, 3.0)
    tts = SpeakingMock(tmp_path, rate=3.0)

    seen_budgets = []

    def recording_describe(budget, feedback):
        seen_budgets.append(budget)
        if feedback is None:
            return " ".join(["word"] * 20)
        return " ".join(["word"] * budget)

    fit_description(gap, recording_describe, tts, model="mock", max_iterations=4)

    # First budget is the gap estimate; the retry budget comes from the
    # measured overrun and must be smaller than the 20-word draft.
    assert len(seen_budgets) >= 2
    assert seen_budgets[1] < 20


@needs_ffmpeg
def test_make_fit_loop_returns_an_agentloop(tmp_path):
    from genblaze_core.agents.loop import AgentLoop

    gap = Gap(0, 0.0, 5.0)
    tts = SpeakingMock(tmp_path)
    loop = make_fit_loop(gap, make_describe_fn(), tts, model="mock")
    assert isinstance(loop, AgentLoop)

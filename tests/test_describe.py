"""Prompt construction and fit-loop tests."""

from __future__ import annotations

import pytest

from overtone.describe import (
    SYSTEM_PROMPT,
    DescriptionSpec,
    build_prompt,
    check_fit,
    measured_rate,
    word_count,
)
from overtone.gaps import Gap


def spec(**kwargs) -> DescriptionSpec:
    base = {"gap": Gap(0, 10.0, 13.0), "word_budget": 8}
    base.update(kwargs)
    return DescriptionSpec(**base)


class TestSystemPrompt:
    def test_forbids_the_stock_openers(self):
        assert "we see" in SYSTEM_PROMPT
        assert "the image shows" in SYSTEM_PROMPT

    def test_requires_technical_content_to_be_read_aloud(self):
        # The whole STEM wedge lives in these instructions.
        assert "x squared plus two x minus three equals zero" in SYSTEM_PROMPT
        assert "open paren" in SYSTEM_PROMPT
        assert "not \"a bar chart is shown\"" in SYSTEM_PROMPT


class TestBuildPrompt:
    def test_states_the_pause_length_and_word_limit(self):
        prompt = build_prompt(spec(word_budget=7))
        assert "3.0 second pause" in prompt
        assert "7 words" in prompt

    def test_includes_surrounding_transcript(self):
        prompt = build_prompt(
            spec(transcript_before="so that gives us the gradient",
                 transcript_after="which is why it converges")
        )
        assert "so that gives us the gradient" in prompt
        assert "which is why it converges" in prompt

    def test_tells_the_model_not_to_duplicate_the_next_line(self):
        prompt = build_prompt(spec(transcript_after="the graph slopes downward"))
        assert "Do not describe anything that line already tells the listener." in prompt

    def test_omits_transcript_sections_when_absent(self):
        prompt = build_prompt(spec())
        assert "The speaker has just said" not in prompt

    def test_passes_recent_descriptions_to_prevent_repetition(self):
        prompt = build_prompt(spec(previous_descriptions=["A title slide.", "A scatter plot."]))
        assert "A scatter plot." in prompt

    def test_only_the_last_three_descriptions_are_carried(self):
        prompt = build_prompt(
            spec(previous_descriptions=["one", "two", "three", "four"])
        )
        assert "one" not in prompt.split("do not repeat:")[-1]
        assert "four" in prompt

    def test_feedback_is_included_on_retries(self):
        prompt = build_prompt(spec(feedback="Your previous description ran too long."))
        assert "Your previous description ran too long." in prompt


class TestFit:
    def test_word_count(self):
        assert word_count("a scatter plot of height against weight") == 7

    def test_measured_rate(self):
        assert measured_rate("one two three four", 2.0) == pytest.approx(2.0)

    def test_zero_duration_rate_is_zero(self):
        assert measured_rate("anything", 0.0) == 0.0

    def test_description_within_the_pause_fits(self):
        outcome = check_fit("short enough", 2.4, Gap(0, 0.0, 3.0))
        assert outcome.fits is True
        assert outcome.feedback is None

    def test_exactly_filling_the_pause_still_fits(self):
        assert check_fit("x", 3.0, Gap(0, 0.0, 3.0)).fits is True

    def test_overrun_does_not_fit(self):
        outcome = check_fit("far too many words for this", 4.5, Gap(0, 0.0, 3.0))
        assert outcome.fits is False
        assert outcome.ratio == pytest.approx(1.5)

    def test_retry_budget_comes_from_the_measured_rate(self):
        # 10 words in 5s is 2 words/sec. A 3s pause at that rate holds 6,
        # trimmed by the 0.9 safety margin to 5.
        text = " ".join(["word"] * 10)
        outcome = check_fit(text, 5.0, Gap(0, 0.0, 3.0))
        assert outcome.recommended_words == 5

    def test_feedback_quotes_both_durations_and_the_new_budget(self):
        text = " ".join(["word"] * 10)
        outcome = check_fit(text, 5.0, Gap(0, 0.0, 3.0))
        assert "5.0 seconds" in outcome.feedback
        assert "3.0 seconds" in outcome.feedback
        assert "at most 5 words" in outcome.feedback

    def test_recommended_budget_is_never_zero(self):
        # A very long rendering against a very short pause must still leave
        # room for one word rather than asking for an empty description.
        outcome = check_fit("word", 30.0, Gap(0, 0.0, 1.5))
        assert outcome.recommended_words >= 1

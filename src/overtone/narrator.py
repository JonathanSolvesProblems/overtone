"""Producing one fitted spoken description for a single gap.

This is where the pieces meet. A description must say something useful about
the picture and fit inside a fixed pause when spoken aloud. Those two goals
pull against each other, so the work is a loop: write, speak, measure, and if
it overran, write it shorter with the measured overrun as feedback.

The loop is Genblaze's :class:`AgentLoop`. Backblaze's own hackathon brief lists
"agentic pipelines that generate, evaluate, retry, and store outputs" as an
example, and this is precisely that: each iteration is a one-step TTS pipeline,
the evaluator is a duration check, and the feedback tightens the next write.

The description-authoring function is injected rather than imported, so the loop
can be exercised end to end against mock providers with no API keys.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from genblaze_core.agents.evaluator import EvaluationResult, Evaluator
from genblaze_core.agents.loop import AgentContext, AgentLoop
from genblaze_core.models.enums import Modality
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.pipeline.result import PipelineResult
from genblaze_core.providers.base import BaseProvider

from overtone.describe import check_fit, word_count
from overtone.ffmpeg import audio_duration
from overtone.gaps import Gap

logger = logging.getLogger("overtone.narrator")


# describe_fn(word_budget, feedback) -> description text. feedback is None on
# the first attempt and carries the overrun message on a retry.
DescribeFn = Callable[[int, str | None], str]


@dataclass
class FittedDescription:
    """The final spoken description for a gap, however many tries it took."""

    gap: Gap
    text: str
    audio_path: Path
    spoken_seconds: float
    fits: bool
    attempts: int
    total_cost_usd: float = 0.0


def _step_audio_path(result: PipelineResult) -> Path:
    """Pull the rendered audio file path out of a one-step TTS result."""
    step = result.run.steps[-1]
    if not step.assets:
        raise ValueError("TTS step produced no asset")
    parsed = urlparse(step.assets[0].url)
    if parsed.scheme != "file":
        raise ValueError(f"Expected a local audio asset, got {step.assets[0].url!r}")
    return Path(url2pathname(parsed.path))


def _step_text(result: PipelineResult) -> str:
    """The description text that this iteration actually spoke."""
    return result.run.steps[-1].prompt or ""


class DurationFitEvaluator(Evaluator):
    """Passes when the spoken description fits inside its gap.

    Measures the rendered audio rather than trusting a word count, then reuses
    :func:`check_fit` so the retry budget is derived from the rate the voice
    actually delivered.
    """

    def __init__(self, gap: Gap, *, tolerance: float = 1.0):
        self._gap = gap
        self._tolerance = tolerance

    def evaluate(self, result: PipelineResult) -> EvaluationResult:
        text = _step_text(result)
        audio_path = _step_audio_path(result)
        spoken = audio_duration(audio_path)
        outcome = check_fit(text, spoken, self._gap, tolerance=self._tolerance)
        return EvaluationResult(
            passed=outcome.fits,
            score=outcome.ratio,
            feedback=outcome.feedback,
            metadata={
                "spoken_seconds": spoken,
                "recommended_words": outcome.recommended_words,
                "audio_path": str(audio_path),
                "text": text,
            },
        )


def _initial_budget(gap: Gap) -> int:
    return gap.word_budget()


def make_fit_loop(
    gap: Gap,
    describe_fn: DescribeFn,
    tts_provider: BaseProvider,
    *,
    model: str,
    params: dict | None = None,
    max_iterations: int = 3,
    tolerance: float = 1.0,
) -> AgentLoop:
    """Build the generate → evaluate → retry loop for one gap.

    ``describe_fn`` is asked for text at a word budget; the budget starts from
    the gap length and tightens to the evaluator's recommendation on each
    retry. ``tts_provider`` is any Genblaze audio provider (real or mock).
    """
    base_params = dict(params or {})

    def factory(ctx: AgentContext) -> Pipeline:
        if ctx.last_evaluation is not None:
            budget = ctx.last_evaluation.metadata.get("recommended_words") or _initial_budget(gap)
            feedback = ctx.last_evaluation.feedback
        else:
            budget = _initial_budget(gap)
            feedback = None

        text = describe_fn(budget, feedback)
        return (
            Pipeline(f"describe-gap-{gap.index}")
            .preflight(False)
            .step(
                tts_provider,
                model=model,
                prompt=text,
                modality=Modality.AUDIO,
                params=base_params,
            )
        )

    return AgentLoop(
        factory,
        DurationFitEvaluator(gap, tolerance=tolerance),
        max_iterations=max_iterations,
    )


def fit_description(
    gap: Gap,
    describe_fn: DescribeFn,
    tts_provider: BaseProvider,
    *,
    model: str,
    params: dict | None = None,
    max_iterations: int = 3,
    tolerance: float = 1.0,
) -> FittedDescription:
    """Run the fit loop for one gap and return the best description produced.

    "Best" is the passing description if one was found, otherwise the shortest
    attempt, which is what the loop leaves as its final iteration.
    """
    loop = make_fit_loop(
        gap,
        describe_fn,
        tts_provider,
        model=model,
        params=params,
        max_iterations=max_iterations,
        tolerance=tolerance,
    )
    outcome = loop.run()

    final_eval = outcome.iterations[-1].evaluation
    text = final_eval.metadata["text"]
    audio_path = Path(final_eval.metadata["audio_path"])
    spoken = final_eval.metadata["spoken_seconds"]

    logger.info(
        "gap %d fitted in %d attempt(s): %.2fs / %.2fs, %d words",
        gap.index,
        len(outcome.iterations),
        spoken,
        gap.duration,
        word_count(text),
    )

    return FittedDescription(
        gap=gap,
        text=text,
        audio_path=audio_path,
        spoken_seconds=spoken,
        fits=outcome.passed,
        attempts=len(outcome.iterations),
        total_cost_usd=outcome.total_cost_usd,
    )

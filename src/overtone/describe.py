"""Description authoring: the prompt, and the loop that makes it fit.

Two concerns live here.

The prompt encodes audio-description craft. The conventions are not arbitrary
house style; they come from the DCMP Description Key and the ACB Audio
Description Project guidelines, and they are what separates a usable track from
a distracting one. The important departure from generic tooling is the handling
of technical content: a description that says "a slide with an equation
appears" is worthless to a blind engineering student, so equations, code and
charts are read out rather than named.

The fit loop is the other half. A description is only correct if it fits the
pause it was written for, and word counts are a poor predictor of spoken
duration because every voice has its own pace. So the loop measures the
rendered audio and, when it overruns, recomputes the budget from the rate that
voice actually delivered rather than guessing again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from overtone.gaps import Gap

# Trim the recomputed budget slightly below the arithmetic fit. Synthesis
# duration varies a little run to run, and a description that lands at exactly
# 100% of the pause will sometimes clip the next line of dialogue.
FIT_SAFETY_MARGIN = 0.9

# Treat anything at or under this fraction of the pause as fitting.
FIT_TOLERANCE = 1.0


SYSTEM_PROMPT = """\
You write audio description for blind and low-vision viewers, following the \
DCMP Description Key and ACB Audio Description Project conventions.

Voice:
- Start with the thing itself, never with a label for it. Say "A line rises \
from left to right" — never "The slide shows a line", "Displayed is", \
"Pictured", "We see", "This is a graph of". Cut every such preamble.
- Present tense, third person, concrete. Report what is visible, not what it \
means. "He frowns", not "he is angry".
- Dense but plain. Every word earns its place because the pause is short. No \
filler, no hedging, no praise, no mention of being an AI or a description.
- Describe only what the soundtrack does not already say. Never restate the \
speaker.

Never describe the colour, font, or background of text, an equation, or a \
slide. The listener needs what it says, not how it looks. Read the content; \
never say "yellow text on a dark slide".

Technical content is read aloud, not named — this is what separates a usable \
track from a useless one:
- Equations: speak them as a person reading aloud would. "x squared plus two x \
minus three equals zero", not "an equation" and not "an equation is shown".
- Code: read it line by line including the punctuation that changes meaning. \
"def quicksort, open paren, arr, close paren, colon".
- Charts and graphs: name the axes once, then give the shape and the numbers \
that carry the point. "Price against quantity. Supply rises, demand falls, and \
they cross at the centre." Not "a chart".
- Diagrams: describe the structure and how the parts connect, in the order a \
reader would trace them. "Three input nodes feed four hidden nodes, which feed \
two outputs." Not "a diagram".
- On-screen text that carries meaning is read verbatim.

Output only the description, as a spoken phrase. No preamble, no quotation \
marks, no trailing notes.\
"""


@dataclass
class DescriptionSpec:
    """Everything needed to write one description."""

    gap: Gap
    word_budget: int
    transcript_before: str = ""
    transcript_after: str = ""
    previous_descriptions: list[str] = field(default_factory=list)
    attempt: int = 0
    feedback: str | None = None

    @property
    def target_seconds(self) -> float:
        return self.gap.duration


def build_prompt(spec: DescriptionSpec) -> str:
    """Build the user-side prompt for one description.

    Surrounding transcript is included so the model can avoid repeating the
    speaker, which is the single most common failure in automated description.
    Recent descriptions are included so it does not restate the same slide in
    different words.
    """
    opening = (
        f"Write one audio description to be spoken aloud in a "
        f"{spec.target_seconds:.1f} second pause. "
        f"Hard limit: {spec.word_budget} words."
    )
    parts: list[str] = [opening]

    if spec.transcript_before:
        parts.append(f"The speaker has just said: \"{spec.transcript_before.strip()}\"")
    if spec.transcript_after:
        parts.append(f"Immediately after the pause the speaker says: \"{spec.transcript_after.strip()}\"")
        parts.append("Do not describe anything that line already tells the listener.")

    if spec.previous_descriptions:
        recent = "; ".join(spec.previous_descriptions[-3:])
        parts.append(f"Already described, do not repeat: {recent}")

    if spec.feedback:
        parts.append(spec.feedback)

    parts.append("The attached frames are from this moment in the video, in order.")
    return "\n\n".join(parts)


def word_count(text: str) -> int:
    return len(text.split())


@dataclass(frozen=True)
class FitOutcome:
    """Result of checking a synthesized description against its pause."""

    spoken_seconds: float
    target_seconds: float
    fits: bool
    ratio: float
    recommended_words: int | None = None
    feedback: str | None = None


def measured_rate(text: str, spoken_seconds: float) -> float:
    """Words per second actually delivered for a given rendering."""
    if spoken_seconds <= 0:
        return 0.0
    return word_count(text) / spoken_seconds


def check_fit(
    text: str,
    spoken_seconds: float,
    gap: Gap,
    *,
    tolerance: float = FIT_TOLERANCE,
    safety: float = FIT_SAFETY_MARGIN,
) -> FitOutcome:
    """Judge whether a rendered description fits, and how to shorten it.

    On overrun the new budget comes from the rate this voice actually
    delivered, not from a static words-per-second constant. That converges in
    one or two attempts instead of oscillating.
    """
    target = gap.duration
    ratio = spoken_seconds / target if target > 0 else float("inf")

    if ratio <= tolerance:
        return FitOutcome(
            spoken_seconds=spoken_seconds,
            target_seconds=target,
            fits=True,
            ratio=ratio,
        )

    rate = measured_rate(text, spoken_seconds)
    recommended = max(1, int(target * rate * safety))

    feedback = (
        f"Your previous description ran {spoken_seconds:.1f} seconds when spoken, "
        f"but the pause is only {target:.1f} seconds. "
        f"Rewrite it in at most {recommended} words, keeping the most important "
        f"visual information and dropping the rest."
    )

    return FitOutcome(
        spoken_seconds=spoken_seconds,
        target_seconds=target,
        fits=False,
        ratio=ratio,
        recommended_words=recommended,
        feedback=feedback,
    )

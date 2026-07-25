"""Frame sampling and visual-change detection.

Two jobs. First, pick the frames a vision model should look at for a given
pause. Second, and just as important, work out whether the picture has actually
changed since the last thing we described.

That second job matters more than it sounds. A lecture often holds one slide on
screen for five minutes. Naively describing every pause produces eight
consecutive descriptions of the same slide, which is worse than useless to a
listener and multiplies the bill for no benefit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from overtone.ffmpeg import extract_frame
from overtone.gaps import Gap

# Frames per gap. Start and middle cover what is on screen during the pause;
# the lookahead catches a slide that changes just as the narrator resumes, so
# the description can introduce what is coming rather than lag behind it.
DEFAULT_FRAMES_PER_GAP = 3

# Hamming distance between two 256-bit difference hashes below which two frames
# are treated as the same picture. On slide-heavy material a genuine change
# lands around 20+ bits while a static slide re-sampled stays near zero, so a
# threshold in between tolerates JPEG noise and a lecturer's laser pointer
# without swallowing a real change.
DEFAULT_SIMILARITY_THRESHOLD = 10

# Largest per-channel mean-colour shift tolerated before a frame counts as new.
# Roughly 7% of the channel range: survives auto-exposure drift, catches a
# genuine cut or slide change.
DEFAULT_COLOUR_THRESHOLD = 18

# Side length of the difference-hash grid. 16 gives a 256-bit hash, enough to
# distinguish two slides that differ only in a line of text, which a coarser
# grid averages away.
DHASH_SIZE = 16


def sample_times(gap: Gap, *, count: int = DEFAULT_FRAMES_PER_GAP, lookahead: float = 0.4) -> list[float]:
    """Choose timestamps to grab frames from for a gap.

    Always returns ``count`` times in ascending order, spanning the pause and
    reaching a little past its end.
    """
    if count <= 1:
        return [gap.start]
    span = gap.duration + lookahead
    step = span / (count - 1)
    return [round(gap.start + step * i, 3) for i in range(count)]


def average_hash(path: str | Path, *, size: int = 8) -> int:
    """Perceptual average hash of an image, as a size*size-bit integer.

    Each pixel is compared against the image's own mean brightness. Kept for
    flat-frame equality checks; :func:`dhash` is the structural signal used for
    change detection because it survives sparse content far better.
    """
    with Image.open(path) as image:
        small = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())

    mean = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= mean:
            bits |= 1 << index
    return bits


def dhash(path: str | Path, *, size: int = DHASH_SIZE) -> int:
    """Difference hash of an image, as a size*size-bit integer.

    Encodes the sign of the horizontal brightness gradient between adjacent
    pixels, so it keys on edges rather than absolute levels. A slide holding one
    line of text produces a distinctive edge pattern that a mean-based hash
    washes out, which is exactly the case that matters for lectures: two slides
    differing only in their text stay well apart under dHash where average hash
    collapses them together.
    """
    with Image.open(path) as image:
        small = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())

    bits = 0
    position = 0
    row_stride = size + 1
    for row in range(size):
        for col in range(size):
            left = pixels[row * row_stride + col]
            right = pixels[row * row_stride + col + 1]
            if left > right:
                bits |= 1 << position
            position += 1
    return bits


def mean_rgb(path: str | Path) -> tuple[int, int, int]:
    """Average colour of an image, per channel."""
    with Image.open(path) as image:
        small = image.convert("RGB").resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())

    count = len(pixels)
    return tuple(round(sum(p[channel] for p in pixels) / count) for channel in range(3))


def hamming(left: int, right: int) -> int:
    """Number of differing bits between two hashes."""
    return ((left ^ right).bit_count())


@dataclass(frozen=True)
class FrameSignature:
    """Structure and colour fingerprint of a frame.

    The structural hash alone is not enough. A flat frame has no gradients, so a
    solid red slide and a solid blue one produce identical (all-zero) difference
    hashes. Carrying the mean colour alongside the structural bits catches
    changes that are obvious to a viewer but invisible to a luminance-only hash.
    """

    bits: int
    colour: tuple[int, int, int]


def signature(path: str | Path) -> FrameSignature:
    """Compute the structure-plus-colour signature of a frame."""
    return FrameSignature(bits=dhash(path), colour=mean_rgb(path))


def colour_distance(left: FrameSignature, right: FrameSignature) -> int:
    """Largest per-channel difference between two signatures."""
    return max(abs(a - b) for a, b in zip(left.colour, right.colour))


@dataclass
class VisualChangeTracker:
    """Decides whether a gap shows something worth describing again.

    Holds the signature of the most recently described frame and compares each
    candidate against it. A frame counts as new if either its layout or its
    colour has moved far enough.
    """

    threshold: int = DEFAULT_SIMILARITY_THRESHOLD
    colour_threshold: int = DEFAULT_COLOUR_THRESHOLD
    last_described: FrameSignature | None = None

    def is_new(self, candidate: FrameSignature) -> bool:
        """True when this frame differs enough from the last described one."""
        if self.last_described is None:
            return True
        if hamming(candidate.bits, self.last_described.bits) > self.threshold:
            return True
        return colour_distance(candidate, self.last_described) > self.colour_threshold

    def accept(self, candidate: FrameSignature) -> None:
        """Record a frame as described, so later frames compare against it."""
        self.last_described = candidate


@dataclass(frozen=True)
class GapFrames:
    """The frames sampled for one gap, and whether they look new."""

    gap: Gap
    paths: list[Path]
    signature: FrameSignature
    is_new: bool


def collect(
    video: str | Path,
    gaps: list[Gap],
    out_dir: str | Path,
    *,
    count: int = DEFAULT_FRAMES_PER_GAP,
    threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
    colour_threshold: int = DEFAULT_COLOUR_THRESHOLD,
    frame_width: int = 768,
) -> list[GapFrames]:
    """Sample frames for every gap and flag which ones show new content.

    Gaps whose picture matches the previously described one come back with
    ``is_new=False``. Callers skip those, which is where most of the cost
    saving on slide-heavy material comes from.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tracker = VisualChangeTracker(threshold=threshold, colour_threshold=colour_threshold)
    results: list[GapFrames] = []

    for gap in gaps:
        paths: list[Path] = []
        for position, at in enumerate(sample_times(gap, count=count)):
            dest = out_dir / f"gap{gap.index:04d}_{position}.jpg"
            paths.append(extract_frame(video, at, dest, width=frame_width))

        # Judge novelty on the middle frame: the start of a pause can still
        # show the outgoing slide mid-transition.
        sig = signature(paths[len(paths) // 2])
        is_new = tracker.is_new(sig)
        if is_new:
            tracker.accept(sig)

        results.append(GapFrames(gap=gap, paths=paths, signature=sig, is_new=is_new))

    return results

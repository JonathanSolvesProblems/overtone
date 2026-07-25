"""The per-video pipeline: B2 in, described master and provenance B2 out.

This is the whole product for a single video, in order:

    download → transcribe → find pauses → sample frames → describe each new
    pause (fitting it to the pause) → mix inline, freeze-frame the rest →
    write the described master, a WebVTT track, a transcript, and a hash-
    verified manifest back beside the original.

Every step that can be a Genblaze provider is one (AssemblyAI for speech, the
TTS providers for voice), storage is the Genblaze S3 backend, and the manifest
is a Genblaze provenance document. Overtone's own code is the connective
tissue: the pause maths, the fit loop, and the timeline mixing that no single
provider offers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from genblaze_core._utils import local_file_url
from genblaze_core.builders.run_builder import RunBuilder
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, StepType
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.step import Step

from overtone.author import VisionAuthor
from overtone.config import Settings
from overtone.costs import Usage, estimate_cost
from overtone.describe import SYSTEM_PROMPT, word_count
from overtone.extended import FreezeInsertion, insert_freeze_descriptions
from overtone.ffmpeg import extract_audio, probe, sha256_file
from overtone.gaps import coverage, find_gaps
from overtone.keyframes import collect
from overtone.mixer import Placement, mix_descriptions
from overtone.narrator import fit_description
from overtone.storage import Bucket, derive_output_keys
from overtone.synth import FailoverTTSProvider
from overtone.vtt import DescriptionCue, build_transcript, build_vtt

logger = logging.getLogger("overtone.orchestrator")

ProgressFn = Callable[[str, dict], None]


@dataclass
class DescriptionRecord:
    """One finished description and everything known about it."""

    gap_index: int
    original_start: float
    final_start: float
    spoken_seconds: float
    text: str
    extended: bool
    attempts: int
    fitted: bool


@dataclass
class VideoResult:
    """Outcome of describing one video."""

    source_key: str
    source_sha256: str
    media_seconds: float
    gap_count: int
    described_count: int
    extended_count: int
    skipped_repeats: int
    descriptions: list[DescriptionRecord]
    cost: dict
    cost_per_minute: float
    elapsed_seconds: float
    outputs: dict[str, str] = field(default_factory=dict)
    manifest_hash: str = ""
    status: str = "described"

    def summary(self) -> str:
        return (
            f"{self.source_key}: {self.described_count} descriptions "
            f"({self.extended_count} extended) over {self.media_seconds:.0f}s, "
            f"${self.cost['total']:.4f} = ${self.cost_per_minute:.4f}/min"
        )


def _emit(progress: ProgressFn | None, event: str, **data) -> None:
    if progress is not None:
        progress(event, data)


def _context_text(words, gap_start: float, gap_end: float, *, window: float = 6.0):
    """Return the speech just before and after a gap, for the author's prompt."""
    before = [w.word for w in words if gap_start - window <= w.end <= gap_start]
    after = [w.word for w in words if gap_end <= w.start <= gap_end + window]
    return " ".join(before), " ".join(after)


def _run_stt(bucket: Bucket, audio_path: Path, settings: Settings):
    """Transcribe local audio via AssemblyAI, returning the Genblaze step."""
    from genblaze_assemblyai import AssemblyAIProvider

    provider = AssemblyAIProvider()
    step = Step(provider="assemblyai", model=settings.stt_model, modality=Modality.TEXT)
    step.params["audio_url"] = local_file_url(audio_path.resolve())

    pred = provider.submit(step)
    while not provider.poll(pred):
        time.sleep(2)
    provider.fetch_output(pred, step)
    return step


def _build_manifest(
    settings: Settings,
    source_key: str,
    source_sha: str,
    stt_step: Step,
    described_path: Path,
    vtt_key: str,
    transcript_key: str,
    records: list[DescriptionRecord],
    usage: Usage,
    cost,
    media_seconds: float,
) -> Manifest:
    """Assemble a hash-verified provenance manifest for the run.

    Records the real speech-to-text step and a compose step whose output is the
    described master, hashed. Every description, with its timing, provider, and
    attempt count, is captured in metadata so the manifest is a complete account
    of how the track was produced.
    """
    described_sha, described_size = sha256_file(described_path)

    compose = Step(provider="overtone", model="compose", modality=Modality.VIDEO)
    compose.step_type = StepType.MIX
    compose.prompt = None
    compose.assets = [
        Asset(
            url=local_file_url(described_path.resolve()),
            media_type="video/mp4",
            sha256=described_sha,
            size_bytes=described_size,
            duration=probe(described_path).duration,
        )
    ]
    compose.metadata["descriptions"] = [
        {
            "gap_index": r.gap_index,
            "final_start": round(r.final_start, 3),
            "spoken_seconds": round(r.spoken_seconds, 3),
            "extended": r.extended,
            "attempts": r.attempts,
            "text": r.text,
        }
        for r in records
    ]
    compose.metadata["vtt_key"] = vtt_key
    compose.metadata["transcript_key"] = transcript_key

    run = (
        RunBuilder("overtone-describe")
        .tenant(settings.b2.bucket if settings.b2 else "local")
        .add_step(stt_step)
        .add_step(compose)
        .meta(
            source_key=source_key,
            source_sha256=source_sha,
            media_seconds=round(media_seconds, 3),
            providers=sorted(
                {m.provider for m in settings.vision_chain}
                | {v.provider for v in settings.tts_chain}
                | {"assemblyai"}
            ),
            description_count=len(records),
            extended_count=sum(1 for r in records if r.extended),
            usage={
                "stt_seconds": round(usage.stt_seconds, 1),
                "vision_calls": usage.vision_calls,
                "tts_chars": usage.tts_chars_by_provider,
            },
            cost_usd=cost.as_dict(),
            cost_per_minute_usd=round(cost.per_minute(media_seconds), 5),
        )
        .build()
    )
    return Manifest.from_run(run)


def describe_video(
    bucket: Bucket,
    source_key: str,
    settings: Settings,
    *,
    work_dir: str | Path,
    max_iterations: int = 3,
    progress: ProgressFn | None = None,
    force: bool = False,
) -> VideoResult:
    """Describe one B2 video and write the results back beside it.

    Set ``force`` to re-describe even when a current manifest already exists.
    """
    started = time.time()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    usage = Usage()

    _emit(progress, "download.start", key=source_key)
    local_video = bucket.download(source_key, work / "source" / Path(source_key).name)
    source_sha, _ = sha256_file(local_video)

    keys = derive_output_keys(source_key)
    if not force:
        from overtone.storage import already_described

        if already_described(bucket, source_key, source_sha):
            _emit(progress, "skipped.already_described", key=source_key)
            return VideoResult(
                source_key=source_key,
                source_sha256=source_sha,
                media_seconds=0.0,
                gap_count=0,
                described_count=0,
                extended_count=0,
                skipped_repeats=0,
                descriptions=[],
                cost={"stt": 0, "vision": 0, "tts": 0, "total": 0},
                cost_per_minute=0.0,
                elapsed_seconds=time.time() - started,
                status="skipped",
            )

    info = probe(local_video)
    media_seconds = info.duration
    _emit(progress, "probe.done", seconds=media_seconds, width=info.width, height=info.height)

    # --- Transcribe -------------------------------------------------------
    _emit(progress, "stt.start")
    audio = extract_audio(local_video, work / "audio.wav")
    stt_step = _run_stt(bucket, audio, settings)
    stt_asset = stt_step.assets[0]
    words = stt_asset.audio.word_timings if stt_asset.audio else []
    usage.add_stt(media_seconds)
    _emit(progress, "stt.done", words=len(words))

    # --- Find describable pauses -----------------------------------------
    gaps = find_gaps(words, media_duration=media_seconds)
    cov = coverage(words, gaps, media_duration=media_seconds)
    _emit(
        progress,
        "gaps.done",
        gaps=len(gaps),
        describable_ratio=round(cov.describable_ratio, 3),
    )

    # --- Sample frames, skipping unchanged slides ------------------------
    frames_dir = work / "frames"
    gap_frames = collect(local_video, gaps, frames_dir) if gaps else []
    new_gaps = [gf for gf in gap_frames if gf.is_new]
    skipped = len(gap_frames) - len(new_gaps)
    _emit(progress, "frames.done", to_describe=len(new_gaps), skipped_repeats=skipped)

    # --- Describe each new pause -----------------------------------------
    inline: list[tuple[DescriptionRecord, Placement]] = []
    extended: list[tuple[DescriptionRecord, FreezeInsertion]] = []
    previous_texts: list[str] = []

    # One TTS provider, chosen fresh per gap so its usage callback and
    # last-clip state don't bleed across gaps.
    for gf in new_gaps:
        gap = gf.gap
        before, after = _context_text(words, gap.start, gap.end)
        author = VisionAuthor(
            gap,
            gf.paths,
            system_prompt=SYSTEM_PROMPT,
            vision_chain=settings.vision_chain,
            transcript_before=before,
            transcript_after=after,
            previous_descriptions=previous_texts,
        )
        tts = FailoverTTSProvider(settings.tts_chain, on_synth=usage.add_tts)

        fitted = fit_description(
            gap,
            author,
            tts,
            model="failover-tts",
            max_iterations=max_iterations,
        )
        # Each authoring attempt is a billed vision call over the gap's frames.
        for _ in range(max(1, author.attempts)):
            usage.add_vision(len(gf.paths))
        previous_texts.append(fitted.text)

        record = DescriptionRecord(
            gap_index=gap.index,
            original_start=gap.start,
            final_start=gap.start,  # adjusted below once extended offsets are known
            spoken_seconds=fitted.spoken_seconds,
            text=fitted.text,
            extended=not fitted.fits,
            attempts=fitted.attempts,
            fitted=fitted.fits,
        )
        if fitted.fits:
            inline.append((record, Placement(at=gap.start, audio=fitted.audio_path, duration=fitted.spoken_seconds)))
        else:
            extended.append((record, FreezeInsertion(at=gap.start, audio=fitted.audio_path)))

        _emit(
            progress,
            "described",
            gap=gap.index,
            extended=not fitted.fits,
            words=word_count(fitted.text),
            attempts=fitted.attempts,
        )

    # --- Compose the described master ------------------------------------
    _emit(progress, "compose.start", inline=len(inline), extended=len(extended))
    described_path = _compose(
        local_video,
        [p for _, p in inline],
        [f for _, f in extended],
        work,
    )

    # --- Timeline: adjust cue starts for any freeze-frame insertions -----
    records = _finalize_timeline(
        [r for r, _ in inline],
        [r for r, _ in extended],
    )

    # --- Emit WebVTT + transcript ----------------------------------------
    cues = [
        DescriptionCue(
            index=i,
            start=r.final_start,
            end=r.final_start + r.spoken_seconds,
            text=r.text,
            extended=r.extended,
        )
        for i, r in enumerate(sorted(records, key=lambda r: r.final_start))
    ]
    vtt_text = build_vtt(cues)
    transcript_text = build_transcript(cues)

    # --- Cost --------------------------------------------------------------
    cost = estimate_cost(usage)

    # --- Provenance manifest ----------------------------------------------
    manifest = _build_manifest(
        settings, source_key, source_sha, stt_step, described_path,
        keys.vtt, keys.transcript, records, usage, cost, media_seconds,
    )
    if not manifest.verify():
        logger.warning("manifest failed verification for %s", source_key)

    # --- Write everything back to B2 -------------------------------------
    _emit(progress, "upload.start")
    outputs: dict[str, str] = {}
    outputs["described_video"] = bucket.upload(
        keys.described_video, described_path, content_type="video/mp4"
    )
    outputs["vtt"] = bucket.upload_text(keys.vtt, vtt_text, content_type="text/vtt")
    outputs["transcript"] = bucket.upload_text(
        keys.transcript, transcript_text, content_type="text/plain"
    )
    outputs["manifest"] = bucket.upload_text(
        keys.manifest, manifest.model_dump_json(indent=2), content_type="application/json"
    )
    _emit(progress, "upload.done", **outputs)

    result = VideoResult(
        source_key=source_key,
        source_sha256=source_sha,
        media_seconds=media_seconds,
        gap_count=len(gaps),
        described_count=len(records),
        extended_count=len(extended),
        skipped_repeats=skipped,
        descriptions=sorted(records, key=lambda r: r.final_start),
        cost=cost.as_dict(),
        cost_per_minute=cost.per_minute(media_seconds),
        elapsed_seconds=time.time() - started,
        outputs=outputs,
        manifest_hash=manifest.canonical_hash,
    )
    _emit(progress, "done", summary=result.summary())
    return result


def _compose(video, placements, insertions, work):
    """Mix inline descriptions, then apply any freeze-frame insertions."""
    described = Path(work) / "described.mp4"
    if placements:
        mixed = mix_descriptions(video, placements, work / "mixed.mp4")
    else:
        mixed = Path(video)

    if insertions:
        return insert_freeze_descriptions(mixed, insertions, described)

    if placements:
        return mixed
    # No descriptions at all: the described master is the source unchanged.
    import shutil

    shutil.copyfile(video, described)
    return described


def _finalize_timeline(inline_records, extended_records):
    """Shift cue start times to account for freeze-frame insertions.

    Inline descriptions sit on the original timeline; each freeze-frame
    insertion pushes everything after it later by its hold duration. Processing
    all descriptions in original-time order and accumulating that offset yields
    the correct final-timeline start for every cue.
    """
    ordered = sorted(
        inline_records + extended_records, key=lambda r: r.original_start
    )
    offset = 0.0
    for record in ordered:
        record.final_start = record.original_start + offset
        if record.extended:
            # The video holds for the spoken length plus the freeze tail.
            from overtone.extended import FREEZE_TAIL

            offset += record.spoken_seconds + FREEZE_TAIL
    return ordered

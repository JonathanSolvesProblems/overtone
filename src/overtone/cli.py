"""Command line entry point.

Four verbs, matching how the tool is actually used:

    overtone doctor            what's configured, before spending anything
    overtone scan   PREFIX     what's describable under a prefix, and what's done
    overtone describe KEY      one video, end to end
    overtone archive PREFIX    the whole prefix, with resume

The archive verb is the one the pitch turns on: point it at a bucket prefix and
it works through every video that is not already described, skipping the rest.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from overtone.config import Settings, load
from overtone.storage import Bucket, derive_output_keys


def _require_b2(settings: Settings) -> None:
    if settings.b2 is None:
        sys.exit("No B2 configuration. Set B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET (see .env.example).")


def _require_providers(settings: Settings) -> None:
    missing = []
    if not settings.has_assemblyai:
        missing.append("AssemblyAI (ASSEMBLYAI_API_KEY)")
    if not settings.vision_chain:
        missing.append("a vision provider (GOOGLE_API_KEY / OPENAI_API_KEY / GMI_CLOUD_API_KEY)")
    if not settings.tts_chain:
        missing.append("a TTS provider (HUME_API_KEY / ELEVENLABS_API_KEY / OPENAI_API_KEY)")
    if missing:
        sys.exit("Missing providers:\n  - " + "\n  - ".join(missing))


def cmd_doctor(settings: Settings, args) -> int:
    print("Overtone configuration")
    print("=" * 24)
    if settings.b2:
        print(f"B2 bucket   : {settings.b2.bucket} ({settings.b2.region})")
    else:
        print("B2 bucket   : NOT CONFIGURED")
    print(f"Speech->text: {'AssemblyAI ' + settings.stt_model if settings.has_assemblyai else 'NOT CONFIGURED'}")
    print(f"Vision chain: {', '.join(str(m) for m in settings.vision_chain) or 'NOT CONFIGURED'}")
    print(f"TTS chain   : {', '.join(str(v) for v in settings.tts_chain) or 'NOT CONFIGURED'}")
    ready = settings.b2 and settings.has_assemblyai and settings.vision_chain and settings.tts_chain
    print()
    print("Ready to describe." if ready else "Not ready — see NOT CONFIGURED above.")
    return 0 if ready else 1


def cmd_scan(settings: Settings, args) -> int:
    _require_b2(settings)
    bucket = Bucket.from_config(settings.b2)
    total = done = 0
    for key in bucket.scan_videos(args.prefix):
        total += 1
        keys = derive_output_keys(key)
        marker = "done" if bucket.exists(keys.manifest) else "todo"
        print(f"  [{marker}] {key}")
        if marker == "done":
            done += 1
    print(f"\n{total} videos under {args.prefix!r}: {done} described, {total - done} to do")
    return 0


def _progress(event: str, data: dict) -> None:
    if event == "described":
        tag = " [extended]" if data.get("extended") else ""
        print(f"    described gap {data['gap']}{tag} ({data['words']}w, {data['attempts']} tries)")
    elif event == "stt.done":
        print(f"    transcript: {data['words']} words")
    elif event == "gaps.done":
        print(f"    gaps: {data['gaps']} (describable {data['describable_ratio']:.0%})")
    elif event == "frames.done":
        print(f"    to describe: {data['to_describe']}, skipped repeats: {data['skipped_repeats']}")
    elif event == "skipped.already_described":
        print("    already described — skipping")


def _describe_one(bucket: Bucket, key: str, settings: Settings, work_root: Path, force: bool):
    from overtone.orchestrator import describe_video

    print(f"\n{key}")
    result = describe_video(
        bucket, key, settings,
        work_dir=work_root / key.replace("/", "_"),
        progress=_progress,
        force=force,
    )
    if result.status == "skipped":
        return result
    print(f"    => {result.described_count} descriptions, "
          f"${result.cost['total']:.4f} (${result.cost_per_minute:.4f}/min), "
          f"{result.elapsed_seconds:.0f}s")
    return result


def cmd_describe(settings: Settings, args) -> int:
    _require_b2(settings)
    _require_providers(settings)
    bucket = Bucket.from_config(settings.b2)
    work_root = Path(args.work)
    _describe_one(bucket, args.key, settings, work_root, args.force)
    return 0


def cmd_archive(settings: Settings, args) -> int:
    _require_b2(settings)
    _require_providers(settings)
    bucket = Bucket.from_config(settings.b2)
    work_root = Path(args.work)

    keys = list(bucket.scan_videos(args.prefix))
    print(f"{len(keys)} videos under {args.prefix!r}")

    described = skipped = 0
    total_cost = 0.0
    total_seconds = 0.0
    for key in keys:
        result = _describe_one(bucket, key, settings, work_root, args.force)
        if result.status == "skipped":
            skipped += 1
        else:
            described += 1
            total_cost += result.cost["total"]
            total_seconds += result.media_seconds

    print("\n" + "=" * 40)
    print(f"described {described}, skipped {skipped}")
    if total_seconds > 0:
        print(f"total ${total_cost:.4f} over {total_seconds/60:.1f} min "
              f"= ${total_cost / (total_seconds/60):.4f}/min")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="overtone",
        description="Generate WCAG audio description for a video archive, in place, on Backblaze B2.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Show what is configured.").set_defaults(func=cmd_doctor)

    p_scan = sub.add_parser("scan", help="List describable videos under a prefix.")
    p_scan.add_argument("prefix", nargs="?", default="", help="B2 key prefix.")
    p_scan.set_defaults(func=cmd_scan)

    p_desc = sub.add_parser("describe", help="Describe one video.")
    p_desc.add_argument("key", help="B2 object key of the source video.")
    p_desc.add_argument("--work", default="work", help="Local working directory.")
    p_desc.add_argument("--force", action="store_true", help="Re-describe even if already done.")
    p_desc.set_defaults(func=cmd_describe)

    p_arch = sub.add_parser("archive", help="Describe every video under a prefix, with resume.")
    p_arch.add_argument("prefix", nargs="?", default="", help="B2 key prefix.")
    p_arch.add_argument("--work", default="work", help="Local working directory.")
    p_arch.add_argument("--force", action="store_true", help="Re-describe even if already done.")
    p_arch.set_defaults(func=cmd_archive)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = load()
    return args.func(settings, args)


if __name__ == "__main__":
    sys.exit(main())

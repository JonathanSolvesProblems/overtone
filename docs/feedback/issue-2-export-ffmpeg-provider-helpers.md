# Export the deterministic-provider ffmpeg helpers that already exist in `_ffmpeg_utils`

**Type:** Feature request (corroborates #172)
**Affects:** `genblaze-core` 0.3.7

## Summary

Building a custom deterministic provider (one that transforms media with ffmpeg
rather than calling a generative API) means re-implementing input resolution,
subprocess handling, output paths, and asset hashing by hand. #172 (ProofRelay)
asked for shared helpers so custom deterministic steps could be "shorter and
safer." The useful part: **those helpers already exist**, in
`genblaze_core.providers._ffmpeg_utils`. They are just private.

I hit the same wall building an audio-description tool with two custom ffmpeg
steps (a timeline-accurate audio mixer and a freeze-frame compositor), and ended
up re-writing thin copies of functions the SDK already ships, purely to avoid
depending on an underscored module.

## What already exists (and is private)

`genblaze_core/providers/_ffmpeg_utils.py`:

- `resolve_ffmpeg(path)` — locate the binary with an actionable error.
- `resolve_input_path(url, extra_roots=...)` — resolve `file://` (validated
  under allowed roots) and `https://` (SSRF-checked) inputs for ffmpeg.
- `run_ffmpeg(cmd, timeout)` — run with a timeout and, notably, **redact
  presigned-URL query strings from logs and error text** (#75). Re-implementing
  this by hand is exactly where a credential leak sneaks in.
- `get_output_path(step_id, ext, output_dir)`.
- `populate_file_asset_integrity(asset, path)` — stream a file to fill
  `asset.sha256` and `asset.size_bytes`.

These are precisely the "safe `file://` / `https://` input loading, output-file
management, byte hashing, and Asset construction" #172 describes.

## Request

Promote these to a public, documented surface, e.g.
`genblaze_core.providers.ffmpeg` (or `genblaze_core.media.ffmpeg`), and reference
them from the "custom provider" guide. Concretely:

- `resolve_ffmpeg`, `resolve_input_path`, `run_ffmpeg`, `get_output_path`,
  `populate_file_asset_integrity` as public API.
- A short cookbook entry: "Write a deterministic ffmpeg provider" showing a
  `SyncProvider.generate()` that resolves inputs, runs ffmpeg, and returns a
  hashed `Asset` using these helpers.

## Why it matters

- Every deterministic media provider needs this exact set. Leaving it private
  guarantees each author re-implements it, and the security-sensitive parts (the
  SSRF check in `resolve_input_path`, the presigned-URL redaction in
  `run_ffmpeg`) are the ones most likely to be gotten wrong in a re-implementation.
- It closes #172 with almost no new code: the implementation exists and is
  already unit-tested; this is largely an export plus a docs page.
- `FFmpegCompositor` and the transform providers already depend on it, so the
  surface is stable enough to commit to publicly.

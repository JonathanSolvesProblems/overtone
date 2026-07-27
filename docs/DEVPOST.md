# Overtone — Devpost submission

> Audio description for a video archive, generated in place, on Backblaze B2.

## Elevator pitch

It's like the audio-description services universities already pay for (3Play,
Verbit) — except it runs across your entire B2 archive for a fraction of a cent
a minute instead of $15–$75, and it never makes you upload a thing.

## The problem

WCAG 2.1 (SC 1.2.5) requires a spoken *audio description* track on prerecorded
video: a narrator describing what's on screen, in the pauses in dialogue. It's
the accessibility requirement everyone skips, because captions are cheap and
description is not. Human describers charge **$15–$75 per finished minute**.

For an archive, that's not expensive, it's impossible. 3Play's enterprise tier
advertises "10 to 10,000+ hours." At $7/min, 10,000 hours is **$4.2M**. So
archives stay non-compliant, or get deleted: in 2017 **UC Berkeley pulled 20,000+
lectures offline** rather than pay to remediate them. Public universities in the
US owe WCAG 2.1 AA compliance under the DOJ's Title II rule; the EU's
Accessibility Act has been binding since June 2025.

## What it does

Point Overtone at a Backblaze B2 bucket prefix. For every video that isn't
already described, it:

1. reads the video **out of B2**,
2. transcribes it (AssemblyAI) to get word-level timings,
3. finds the pauses a describer would speak into,
4. samples keyframes, skipping slides that haven't changed,
5. **describes each pause** with a vision model — reading equations and code
   *aloud* ("y equals m x plus b", not "an equation is shown"),
6. **fits** the narration to the pause: speak it, measure it, and if it ran long,
   rewrite it shorter (a Genblaze `AgentLoop`),
7. mixes it into the audio (ducking the program underneath), and for a pause too
   short to fit, freezes the frame and plays the full description (WCAG 1.2.7),
8. writes the described master, a WebVTT track, a transcript, and a hash-verified
   provenance manifest **back into the same bucket, beside the original**.

Nothing leaves your storage. FERPA-covered recordings never go to a vendor.

## The one number

Measured, not projected: a 3-minute segment of a real MIT OpenCourseWare lecture
(18.03 Differential Equations) cost **$0.08 to describe — about $0.03 per finished
minute** end to end, against a human describer's **$15–$75**. The pipeline read
the board ("A line extends from the point labeled x y upward"), found 8
describable pauses, and wrote the results back to B2. Most of the cost is the
premium ElevenLabs voice; OpenAI TTS at archive scale brings it toward a cent a
minute. The comparison is computed from metered usage against published provider
rates and shown in the app next to every video.

## Why it's different from the AI-description tools that already exist

AI audio description is a real category (3Play, Verbit, ViddyScribe,
MediaScribe). I'm not claiming to invent it. What none of them do, and what this
hackathon is actually about, is run **where the archive already lives**:

- **Locality.** Every one of them is a service you upload your archive *to*, then
  pay egress and a per-minute fee. Overtone runs against B2 directly and writes
  the described master, VTT, transcript, and manifest back beside each original.
  FERPA-covered recordings never leave storage the institution already trusts.
  This is the B2-as-system-of-record story the judging criteria reward.
- **Price at archive scale.** Per-minute, human-in-the-loop pricing cannot reach
  10,000 hours. Overtone's cost scales with compute, so the unit of work is the
  *archive*, not the video.
- **Technical content.** Generic description says "a slide with an equation
  appears." 3Play's own STEM guidance tells customers to skip description and
  commission a MathML transcript instead. Overtone reads the board: equations as
  spoken math, code line by line.

And it **fits the workflow that already exists**: the WebVTT it writes is exactly
what Panopto and Kaltura ingest as an audio-description track (plain text, time-
based cues, no markup), so the output drops into the platform a university
already runs while the media stays in B2.

## How it uses Backblaze B2

B2 is the system of record on **both** sides. Overtone lists and streams source
video out of B2, and writes four artifacts back beside each original via
`genblaze-s3`'s `S3StorageBackend`: the described `.mp4`, a WebVTT descriptions
track, a reviewer transcript, and a provenance manifest. Resume is
content-addressed against that manifest (it stores the source SHA-256), so an
archive sweep is idempotent: unchanged videos are skipped, replaced ones are
re-described. The hosted app serves described masters straight from B2 via
short-lived presigned URLs.

## How it uses Genblaze

Genblaze orchestrates every generative step:

- **Providers:** AssemblyAI (speech-to-text with word timings), ElevenLabs and
  OpenAI TTS (voice, as a failover chain), OpenAI/Google/GMI vision (description).
- **Storage:** `genblaze-s3` `S3StorageBackend` and durable/presigned URL
  handling for B2.
- **AgentLoop:** the generate → evaluate → retry loop that fits each description
  to its pause — exactly the "agentic pipelines that generate, evaluate, retry,
  and store outputs" the brief calls for.
- **Manifest:** a hash-verified provenance document recording how every output
  was produced, written to B2 as the resume marker.

## Providers and models

| Role | Provider | Model |
|------|----------|-------|
| Speech-to-text | AssemblyAI | `universal-2` |
| Description (vision) | OpenAI (Google / GMI Cloud supported) | `gpt-4o-mini` |
| Voice | ElevenLabs (OpenAI TTS failover) | `eleven_multilingual_v2` / `tts-1` |
| Storage | Backblaze B2 | S3-compatible |

## How I built it

A Python package with a CLI (`doctor` / `scan` / `describe` / `archive`) and a
FastAPI demo app. The generative steps are Genblaze providers; the connective
logic that no single provider offers is Overtone's own and is the interesting
part: dialogue-gap detection that merges overlapping speakers, a difference-hash
change detector that avoids re-describing a static slide, the fit loop that
derives its retry budget from the rate the voice actually delivered, and
timeline-accurate mixing with audio ducking. ~140 tests, including ffmpeg
integration and a fit loop proven to converge against a mock provider.

## Challenges

- **Fitting narration to a pause.** Word counts predict spoken duration badly
  because every voice has its own pace, so the loop measures the rendered audio
  and recomputes the budget from the actual rate. It converges in one or two
  tries instead of oscillating.
- **Not describing the same slide eight times.** An average-perceptual-hash
  couldn't tell two equations apart (4 bits different); a difference hash could
  (21 bits). That switch is what makes archive-scale cost real.
- **Cross-provider vision.** The canonical multimodal block works on the
  OpenAI-wire connectors but was rejected by the Google one; I wrote an adapter
  so all three providers work behind one call, and filed the bug upstream
  ([genblaze #194](https://github.com/backblaze-labs/genblaze/issues/194)). The
  maintainers merged a fix implementing exactly the translation I proposed
  ([PR #217](https://github.com/backblaze-labs/genblaze/pull/217)), so Gemini
  vision now works with the canonical blocks in the SDK itself.

## Platform engagement

Building on a days-old SDK surfaced real issues, and I fed them back. Two filed:
the Gemini multimodal rejection (**#194**, since **merged** as PR #217) and a
request to export the deterministic-provider ffmpeg helpers that already exist
privately (**#195**). One of my two pieces of feedback shipped in genblaze during
the hackathon.

## What's next

Real EHR-of-video-scale pilots (a department's Panopto archive), a human-review
queue that lets an accessibility office approve or edit before publish, and
speaker-diarized description that names who's on screen.

## Links

- **App:** (deployed URL)
- **Repo:** https://github.com/JonathanSolvesProblems/overtone
- **Demo video:** (link)

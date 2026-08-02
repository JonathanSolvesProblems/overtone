# Overtone

**Audio description for a video archive, generated in place, on Backblaze B2.**

[Watch the 2-minute demo](https://www.youtube.com/watch?v=H294cHpI5jA) · [Live app](https://overtone.jonathanandrei.com) · [Source](https://github.com/JonathanSolvesProblems/overtone)

[WCAG 2.1 (SC 1.2.5)](https://www.w3.org/WAI/WCAG21/Understanding/audio-description-prerecorded.html)
requires a spoken *audio description* track on prerecorded video: a narrator
describing what is on screen, spoken into the pauses in dialogue. It is the
accessibility requirement almost everyone skips, because captions are cheap and
description is not. Human describers charge
[**$15–$75 per finished minute**](https://www.3playmedia.com/blog/how-much-does-audio-description-cost/),
so a university with twenty thousand lecture recordings faces a bill in the
millions and the archive stays non-compliant, or gets taken offline. In 2017,
[UC Berkeley removed 20,000+ lectures](https://www.insidehighered.com/news/2017/03/06/u-california-berkeley-delete-publicly-available-educational-content)
from public access rather than pay to remediate them. US public entities owe
WCAG 2.1 AA under the
[DOJ's ADA Title II rule](https://www.ada.gov/resources/2024-03-08-web-rule/) by
April 26, 2027.

Overtone makes describing an entire archive cheap enough to actually do, and it
does the work **where the archive already lives**. It reads each video out of a
Backblaze B2 bucket, generates a description track, and writes the described
master back beside the original. Nothing is uploaded to a third-party vendor;
FERPA-covered recordings never leave the storage you already trust.

**Measured on real footage:** a 3-minute segment of an actual MIT OpenCourseWare
lecture (18.03 Differential Equations, a chalkboard class) cost **about nine cents
to describe, roughly three cents per finished minute** end to end, against a human
describer's **$15–$75**. It reads the board with GPT-4o and voices the track with
OpenAI TTS; short clips cost more per minute because fixed transcription overhead
does not amortize. Either way it is hundreds to thousands of times cheaper than a person.

---

## What makes it different

Commercial AI description tools exist (3Play, Verbit, Visonic). They all share
two limits Overtone is built to break:

1. **They price per minute, with a human in the loop, so they cannot serve an
   archive.** 10,000 hours at $7/min is $4.2M. Overtone's cost scales with
   compute, not human minutes, so the unit of work is the *archive*, not the
   video. Point it at a bucket prefix; it describes everything not already done.

2. **They require upload.** You ship them your video and pay egress. Overtone
   runs against B2 directly and writes results back in place.

And one thing generic description gets wrong that matters for lectures:

3. **Technical content.** Generic tools say *"a slide with an equation
   appears."* That is useless to a blind engineering student. Overtone reads the
   board: equations as spoken math (*"y equals m x plus b"*), diagrams traced
   node by node, on-screen text read verbatim, not just that a slide is present.

Batch AI-description SaaS does exist (ViddyScribe, MediaScribe, Maestra). The
distinction is where the work runs: those are services you upload your archive
*to*. Overtone runs against B2 in place and writes results back beside each
original, so the media never leaves storage you control.

### Fits the workflow you already have

Overtone emits a plain-text WebVTT `descriptions` track. That is exactly the
format the two dominant university video platforms ingest: both
[Panopto](https://support.panopto.com/s/article/How-to-Add-Audio-Descriptions)
and [Kaltura](https://knowledge.kaltura.com/help/extended-audio-description)
accept an audio-description VTT that begins with `WEBVTT` and carries plain,
time-based cues (they reject in-cue markup, which is why the output is kept tag
free). So the described track drops straight into the system a university
already runs, and the master and manifest stay in B2.

---

## How it works

For each video:

1. **Read** the video out of B2 (source, not just sink).
2. **Transcribe** with AssemblyAI to get word-level timings.
3. **Find the pauses** a describer would speak into (silences long enough to
   hold a useful phrase), merging overlapping speakers so a gap is never
   invented where two people talk at once.
4. **Sample keyframes** for each pause, using a difference hash to **skip slides
   that have not changed**: a slide held for five minutes is described once,
   not eight times.
5. **Describe** each new pause with a vision model, in a prompt that reads
   technical content aloud, sized to the length of the pause.
6. **Fit** the narration: speak it, measure the real audio, and if it overran
   the pause, rewrite it shorter using the rate the voice actually delivered.
   This is a **Genblaze `AgentLoop`** (generate → evaluate → retry).
7. **Compose**: mix the descriptions into the pauses with the program audio
   ducked underneath, and for a pause too short to fit even a trimmed
   description, freeze the frame and play the full description (WCAG 1.2.7
   extended description).
8. **Write back** to B2, beside the original: the described `.mp4`, a WebVTT
   `descriptions` track, a reviewer transcript, and a hash-verified provenance
   manifest.

A re-run is cheap because the manifest records the source's SHA-256: an
unchanged video is skipped, a replaced one is re-described.

---

## Providers and models

| Role | Provider | Model | Via |
|------|----------|-------|-----|
| Speech-to-text (word timings) | **AssemblyAI** | `universal-2` | Genblaze connector |
| Description (vision) | **OpenAI** (or Google / GMI Cloud) | `gpt-4o` | Genblaze chat + Overtone's multimodal adapter |
| Voice | **ElevenLabs** (OpenAI TTS failover) | `eleven_multilingual_v2` / `tts-1` | Genblaze connectors |
| Storage | **Backblaze B2** | S3-compatible | `genblaze-s3` |

Vision and voice each run through a **failover chain**: if the primary provider
rate-limits or errors, the next one takes over, so an archive-scale run does not
die on a single transient failure.

## How it uses Backblaze B2 and Genblaze

**Backblaze B2** is the system of record on *both* sides. Overtone lists and
streams source video out of B2, and writes the described master, WebVTT track,
transcript, and provenance manifest back into the same bucket beside each
original (via `genblaze-s3`'s `S3StorageBackend`). Resume is content-addressed
against the manifest stored in B2, so an archive sweep is idempotent.

**Genblaze** orchestrates every generative step: the AssemblyAI, ElevenLabs, and
OpenAI providers; the S3 storage backend; the `AgentLoop` that fits each
description to its pause; and the hash-verified `Manifest` that records exactly
how every output was produced.

---

## Run it yourself

Requires Python 3.11+ and **ffmpeg** on the PATH.

```bash
pip install -e .            # or: pip install -e ".[web,dev]"
cp .env.example .env        # fill in B2 + provider keys
overtone doctor             # check what's configured
```

Then, against your own bucket:

```bash
overtone scan     lectures/            # what's describable, and what's done
overtone describe lectures/week3.mp4   # one video
overtone archive  lectures/            # the whole prefix, with resume
```

Outputs land beside each source in B2:

```
lectures/week3.mp4                     # your original, untouched
lectures/week3.described.mp4           # + audio description track
lectures/week3.ad.vtt                  # WebVTT descriptions track
lectures/week3.ad-transcript.txt       # reviewer transcript
lectures/week3.ovtmanifest.json        # hash-verified provenance
```

## The hosted demo

A small FastAPI app (`overtone-web`) shows described results and lets you run a
live describe on a curated clip, watching the pipeline work over server-sent
events. A spend cap, per-client rate limit, and video allowlist keep it safe on
a public URL. See [`Dockerfile`](Dockerfile) and [`fly.toml`](fly.toml).

A 2-minute walkthrough is on YouTube: <https://www.youtube.com/watch?v=H294cHpI5jA>

## Tests

```bash
pytest        # ~150 tests: timing logic, mixing, change detection,
              # the fit loop against a mock provider, cost model, guard
```

The media tests shell out to real ffmpeg; the fit-loop tests prove convergence
against a mock TTS provider with no API keys.

## Sources

Every factual claim in this README and in the demo video is backed by a public
source. The demo shows each of these pages on screen as it makes the claim.

- **WCAG 2.1 requires audio description (SC 1.2.5, Level AA)** — W3C, *Understanding
  SC 1.2.5: Audio Description (Prerecorded)*:
  <https://www.w3.org/WAI/WCAG21/Understanding/audio-description-prerecorded.html>
- **US public entities must meet WCAG 2.1 AA by April 26, 2027 (populations 50,000+)** —
  U.S. Department of Justice, fact sheet on the ADA Title II web and mobile app rule:
  <https://www.ada.gov/resources/2024-03-08-web-rule/>
- **Human audio description costs $15–$75 per finished minute** — 3Play Media,
  *How Much Does Audio Description Cost?*:
  <https://www.3playmedia.com/blog/how-much-does-audio-description-cost/>
- **UC Berkeley removed 20,000+ lectures from public access (2017) rather than
  remediate them** — Inside Higher Ed, *Berkeley Will Delete Online Content* (March 2017):
  <https://www.insidehighered.com/news/2017/03/06/u-california-berkeley-delete-publicly-available-educational-content>
- **Real-footage demo sample** — MIT OpenCourseWare, 18.03 Differential Equations
  (Spring 2010), © MIT, used under CC BY-NC-SA 4.0; the described version is shared
  under the same license:
  <https://ocw.mit.edu/courses/18-03-differential-equations-spring-2010/>

## License

MIT.

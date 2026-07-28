# Standalone `chat()` helpers surface `retry_after` but don't back off on 429, so image-heavy vision loops die on tier-limited keys

> **Resolved:** filed as genblaze #221 and merged as PR #229, which added opt-in
> rate-limit backoff to the `chat()` / vision helpers (plus a follow-up to
> disable SDK-internal retry when the caller manages backoff).

**Type:** Feature request (developer experience / production reliability)
**Affects:** `genblaze-openai` 0.3.3, `genblaze-google` 0.3.3 (the standalone `chat()` / `achat()` helpers)

## Summary

The standalone `chat()` helpers are the documented way to "drive media steps
from an LLM", and in practice they're also how you run a vision model over video
frames. At archive scale that means many multimodal calls in quick succession,
which reliably trips a provider's tokens-per-minute ceiling. When it does,
`chat()` computes `retry_after_from_response(exc)` and attaches it to the
`ProviderError` — but then raises instead of waiting. So every caller has to
re-implement rate-limit backoff around the helper, even though the SDK already
extracted the retry hint.

The `BaseProvider` pipeline path has a `RetryPolicy`; the `chat()` convenience
path, which the docs point you to for exactly this use case, does not.

## How I hit it

Describing a real 3-minute MIT OpenCourseWare lecture (many dialogue pauses →
many vision calls) against a tier-1 OpenAI key (200k TPM). Each `gpt-4o-mini`
call carries a few image tiles (~3k tokens), so the run saturates the minute
budget and every subsequent `chat()` raises:

```
Error code: 429 - Rate limit reached ... on tokens per min (TPM):
Limit 200000, Used 200000, Requested 2954. Please try again in 886ms.
```

`chat()` raises this immediately. Because it doesn't wait, the whole describe
run dies on the first sustained limit unless the caller wraps it. I ended up
writing an escalating-backoff retry loop around `chat()` (honor the provider's
`try again in N` hint early, then grow the wait to clear a full 60s window)
purely to make an archive-scale run survivable.

## What I'd like

Any of these would close the gap; the first is ideal:

1. **Opt-in rate-limit backoff in the helpers.** e.g.
   `chat(..., retry_on_rate_limit=True)` (or honoring a passed `RetryPolicy`)
   that waits `retry_after` and retries up to N times before raising. The SDK
   already has `retry_after_from_response` and a `RetryPolicy` type, so most of
   the pieces exist.
2. **A tiny documented helper** callers can wrap `chat()` with, so everyone
   isn't reinventing the same backoff.
3. **At minimum, a docs note** on the `chat()` reference: "these helpers do not
   retry; wrap them for rate-limited/production loops," with the recommended
   pattern.

## Why it matters

Rate limits aren't an edge case at archive scale — they're the common case, and
the first thing a real batch run hits. The retry hint is already parsed; not
acting on it (or not documenting that the caller must) turns a recoverable
condition into a failed run for anyone driving vision or TTS through `chat()`
over more than a handful of items.

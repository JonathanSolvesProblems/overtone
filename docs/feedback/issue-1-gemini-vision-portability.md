# `genblaze-google` chat() rejects the canonical `ImageURLContent` block, breaking multimodal provider portability

**Type:** Bug report
**Affects:** `genblaze-google` 0.3.3, `genblaze-core` 0.3.7

## Summary

The whole point of the unified `chat()` surface is to swap providers without
rewriting orchestration. For **text** that holds. For **vision it does not**: a
`ChatMessage` carrying the canonical `ImageURLContent` block works on the
OpenAI-wire connectors (OpenAI, GMI Cloud) but is rejected by `genblaze-google`
before any request is made. Worse, the workaround the error message itself
recommends is rejected identically.

This surfaced building an app that describes video frames with a vision model
and wants Gemini and GPT-4o-mini interchangeable behind one code path.

## Reproduction

```python
from genblaze_core.models.chat import ChatMessage, ImageURLContent, ImageURLRef, TextContent

DATA_URI = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="  # any image
msg = ChatMessage(role="user", content=[
    TextContent(text="Describe this frame."),
    ImageURLContent(image_url=ImageURLRef(url=DATA_URI)),
])

# 1. Google, canonical typed block
from genblaze_google import chat as gchat
gchat("gemini-2.5-flash", messages=[msg], api_key="x")
# -> ProviderError: "Gemini does not accept ImageURLContent. Pass media as
#    inline_data (base64) or file_data (File API URI) via raw dict messages ..."

# 2. Google, the raw-dict workaround the error above recommends
raw = {"role": "user", "content": [
    {"type": "text", "text": "Describe this frame."},
    {"type": "image_url", "image_url": {"url": DATA_URI}},
]}
gchat("gemini-2.5-flash", messages=[raw], api_key="x")
# -> SAME ProviderError. The raw dict is coerced back to ChatMessage, so the
#    image_url block re-materializes as ImageURLContent and is rejected again.

# 3. OpenAI, same canonical block -> passes normalization, reaches the API
from genblaze_openai import chat as ochat
ochat("gpt-4o-mini", messages=[msg], api_key="x")   # -> 401 auth (i.e., it was sent)
```

| Path | Result |
|------|--------|
| Google, `ImageURLContent` | Rejected pre-flight |
| Google, raw OpenAI-shaped dict (as the error suggests) | Rejected pre-flight, identical error |
| OpenAI / GMI, `ImageURLContent` | Accepted, request sent |

## Why this matters

- It breaks the SDK's headline promise. A pipeline written against the canonical
  content blocks is portable across text providers but silently non-portable the
  moment an image block is added, and the break is provider-specific.
- The remediation the error message points users to (raw dict messages) does not
  work, because `_normalize_to_gemini` coerces every dict back through
  `ChatMessage(**m)`, so the block is re-validated into `ImageURLContent` and
  hits the same `raise`.
- The only shape that gets through is Gemini's native `{"role": ..., "parts":
  [{"inline_data": {...}}]}`, which is exactly the provider-specific detail the
  abstraction exists to hide, and it is undocumented as the escape hatch.

## Proposed fix

Translate `ImageURLContent` in `_normalize_to_gemini` the way the OpenAI
connector already consumes it, instead of raising:

- `data:` URI → Gemini `inline_data` (`{mime_type, data}` with the base64
  payload split from the header).
- `https://` URL → download and inline, or map to `file_data` if the File API
  URI form is available.

That is the same translation each connector already does from the canonical
block to its own wire shape; Google is the one that raises instead. Until then,
at minimum fix the error message, since the raw-dict workaround it recommends
does not actually work.

## Workaround in the meantime

Build a raw Gemini turn with native `parts` / `inline_data` and pass it as a
dict, bypassing the typed blocks entirely. It reaches the API, but it means the
Google path cannot share the same message-construction code as the OpenAI path,
which is the portability the SDK is supposed to provide.

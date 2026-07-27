"""The hosted demo: a small FastAPI app judges can open and use.

It does two things. It shows described results already sitting in B2 (play the
master, read the descriptions, see the cost against a human describer, inspect
the provenance manifest), and it lets a visitor run a live describe on a curated
demo clip and watch the pipeline work in real time over server-sent events.

The live path is real, not simulated: it calls the same orchestrator the CLI
does, against real providers and the real bucket. The :mod:`overtone.guard`
limits keep that safe to expose on a public URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

from overtone.config import Settings, load
from overtone.costs import HUMAN_AD_PER_MINUTE_HIGH, HUMAN_AD_PER_MINUTE_LOW
from overtone.guard import Guard, NotAllowed, RateLimited, SpendExceeded
from overtone.storage import Bucket, derive_output_keys

logger = logging.getLogger("overtone.web")

STATIC_DIR = Path(__file__).parent / "webstatic"

app = FastAPI(title="Overtone", docs_url=None, redoc_url=None)

_settings: Settings | None = None
_bucket: Bucket | None = None
_guard = Guard()


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load()
    return _settings


def bucket() -> Bucket:
    global _bucket
    if _bucket is None:
        cfg = settings().b2
        if cfg is None:
            raise RuntimeError("B2 is not configured")
        _bucket = Bucket.from_config(cfg)
    return _bucket


def _client_id(request: Request) -> str:
    # Honour the proxy header Fly/most PaaS set, else the socket peer.
    fwd = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/favicon.svg")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/og.png")
def og_image() -> FileResponse:
    return FileResponse(STATIC_DIR / "og.png", media_type="image/png")


@app.get("/api/health")
def health() -> JSONResponse:
    s = settings()
    return JSONResponse(
        {
            "ok": True,
            "b2": bool(s.b2),
            "stt": s.has_assemblyai,
            "vision": [str(m) for m in s.vision_chain],
            "tts": [str(v) for v in s.tts_chain],
            "budget_remaining_usd": round(_guard.remaining_budget(), 2),
        }
    )


@app.get("/api/videos")
def list_videos(prefix: str = "demo/") -> JSONResponse:
    """List demo videos and whether each has a described result."""
    b = bucket()
    out = []
    for key in b.scan_videos(prefix):
        keys = derive_output_keys(key)
        described = b.exists(keys.manifest)
        entry = {"key": key, "described": described}
        if described:
            try:
                manifest = json.loads(b.read_text(keys.manifest))
                md = manifest["run"]["metadata"]
                entry["media_seconds"] = md.get("media_seconds")
                entry["description_count"] = md.get("description_count")
                entry["cost_per_minute"] = md.get("cost_per_minute_usd")
            except Exception as exc:  # noqa: BLE001 — a bad manifest just omits extras
                logger.debug("could not read manifest metadata for %s: %s", key, exc)
        out.append(entry)
    return JSONResponse({"videos": out})


@app.get("/api/result")
def result(key: str) -> JSONResponse:
    """Return everything the viewer needs for one described video."""
    b = bucket()
    keys = derive_output_keys(key)
    if not b.exists(keys.manifest):
        return JSONResponse({"error": "not described yet"}, status_code=404)

    manifest = json.loads(b.read_text(keys.manifest))
    md = manifest["run"]["metadata"]
    descriptions = []
    for step in manifest["run"]["steps"]:
        if step.get("metadata", {}).get("descriptions"):
            descriptions = step["metadata"]["descriptions"]
            break

    media_min = (md.get("media_seconds") or 0) / 60.0
    return JSONResponse(
        {
            "key": key,
            "described_url": b.presigned_url(keys.described_video),
            "original_url": b.presigned_url(key),
            "vtt": b.read_text(keys.vtt),
            "descriptions": descriptions,
            "media_seconds": md.get("media_seconds"),
            "providers": md.get("providers"),
            "cost": md.get("cost_usd"),
            "cost_per_minute": md.get("cost_per_minute_usd"),
            "human_cost_low": round(media_min * HUMAN_AD_PER_MINUTE_LOW, 2),
            "human_cost_high": round(media_min * HUMAN_AD_PER_MINUTE_HIGH, 2),
            "manifest_hash": manifest.get("canonical_hash", ""),
            "output_keys": keys.all(),
            "bucket": settings().b2.bucket if settings().b2 else "",
        }
    )


@app.get("/api/describe")
async def describe_stream(request: Request, key: str) -> StreamingResponse:
    """Run a live describe and stream progress as server-sent events."""
    client = _client_id(request)
    try:
        _guard.authorize(client, key)
    except (SpendExceeded, RateLimited, NotAllowed) as exc:
        # Capture the message now: Python clears the ``as exc`` binding at the
        # end of the except block, so the async closure must not reference it.
        message = str(exc)

        async def error_event():
            yield _sse("error", {"message": message})

        return StreamingResponse(error_event(), media_type="text/event-stream")

    return StreamingResponse(_run_describe(key), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _run_describe(key: str):
    """Bridge the synchronous orchestrator to an async SSE stream.

    The orchestrator runs in a worker thread and pushes progress events onto a
    queue; this coroutine drains the queue and formats each as an SSE frame.
    """
    from overtone.orchestrator import describe_video

    events: queue.Queue = queue.Queue()
    done = threading.Event()
    holder: dict = {}

    def on_progress(event: str, data: dict) -> None:
        events.put((event, data))

    def worker() -> None:
        try:
            work = Path("work") / "web" / key.replace("/", "_")
            res = describe_video(
                bucket(), key, settings(),
                work_dir=work, progress=on_progress, force=True, cleanup=True,
            )
            holder["result"] = res
            _guard.record_spend(res.cost["total"])
        except Exception as exc:
            holder["error"] = str(exc)
            logger.exception("live describe failed for %s", key)
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True, name="overtone-web-describe").start()

    yield _sse("start", {"key": key})
    while not (done.is_set() and events.empty()):
        try:
            event, data = events.get(timeout=0.25)
            yield _sse(event, data)
        except queue.Empty:
            # Heartbeat keeps intermediaries from closing an idle stream.
            yield ": keep-alive\n\n"
        await asyncio.sleep(0)

    if "error" in holder:
        yield _sse("error", {"message": holder["error"]})
    else:
        res = holder["result"]
        yield _sse("complete", {
            "key": key,
            "described_count": res.described_count,
            "extended_count": res.extended_count,
            "cost": res.cost,
            "cost_per_minute": res.cost_per_minute,
            "elapsed_seconds": round(res.elapsed_seconds, 1),
        })


def run() -> None:
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    run()

"""B2 as the system of record on both sides.

The design principle of the whole project lives here: the archive is read from
B2 and the described masters are written back into the same bucket, next to
their originals, never shipped to a third party. A university's FERPA-covered
recordings never leave the storage they already trust.

Output keys mirror the source so an operator browsing the bucket sees the
described track sitting beside the lecture it belongs to. The manifest doubles
as a resume marker: if one already exists for the source's current bytes, the
video is skipped, which is what makes a re-run over a 10,000-video archive cheap
instead of catastrophic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from genblaze_s3 import S3StorageBackend

from overtone.config import B2Config

logger = logging.getLogger("overtone.storage")

# Video extensions worth scanning for. Deliberately conservative; an archive
# holds plenty of non-video objects (manifests, thumbnails, sidecars) and we
# must not try to describe those.
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"})

# Suffixes Overtone itself writes. Skipped on scan so a re-run never treats its
# own described output as fresh source material.
_OUTPUT_SUFFIXES = (".described.mp4", ".ad.vtt", ".ad-transcript.txt", ".ovtmanifest.json")


@dataclass(frozen=True)
class OutputKeys:
    """Where each artifact for a source video is written, relative to source."""

    described_video: str
    vtt: str
    transcript: str
    manifest: str

    def all(self) -> list[str]:
        return [self.described_video, self.vtt, self.transcript, self.manifest]


def _strip_video_ext(key: str) -> str:
    path = Path(key)
    return key[: -len(path.suffix)] if path.suffix else key


def derive_output_keys(source_key: str) -> OutputKeys:
    """Compute the sibling keys for a source video's outputs."""
    stem = _strip_video_ext(source_key)
    return OutputKeys(
        described_video=f"{stem}.described.mp4",
        vtt=f"{stem}.ad.vtt",
        transcript=f"{stem}.ad-transcript.txt",
        manifest=f"{stem}.ovtmanifest.json",
    )


def is_overtone_output(key: str) -> bool:
    """True when a key is something Overtone produced, not source video."""
    return any(key.endswith(suffix) for suffix in _OUTPUT_SUFFIXES)


def is_describable_video(key: str) -> bool:
    """True when a key looks like a source video we should describe."""
    if is_overtone_output(key):
        return False
    return Path(key).suffix.lower() in VIDEO_EXTENSIONS


class Bucket:
    """A thin, intention-revealing wrapper over the Genblaze S3 backend."""

    def __init__(self, backend: S3StorageBackend):
        self._backend = backend

    @classmethod
    def from_config(cls, cfg: B2Config) -> Bucket:
        backend = S3StorageBackend.for_backblaze(
            cfg.bucket,
            region=cfg.region,
            key_id=cfg.key_id,
            app_key=cfg.app_key,
        )
        return cls(backend)

    @property
    def backend(self) -> S3StorageBackend:
        return self._backend

    def scan_videos(self, prefix: str = "") -> Iterator[str]:
        """Yield every describable source-video key under ``prefix``.

        Pages through the whole listing, so it scales to an archive far larger
        than one ``ListObjectsV2`` response.
        """
        token: str | None = None
        while True:
            page = self._backend.list(prefix, continuation_token=token)
            for entry in page.entries:
                if is_describable_video(entry.key):
                    yield entry.key
            token = page.next_token
            if not token:
                break

    def download(self, key: str, dest: str | Path) -> Path:
        """Stream an object down to a local file."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in self._backend.stream(key):
                handle.write(chunk)
        return dest

    def upload(self, key: str, path: str | Path, *, content_type: str) -> str:
        """Upload a local file and return its durable URL."""
        path = Path(path)
        with path.open("rb") as handle:
            self._backend.put(key, handle, content_type=content_type)
        return self._backend.get_durable_url(key)

    def upload_text(self, key: str, text: str, *, content_type: str) -> str:
        import io

        self._backend.put(key, io.BytesIO(text.encode("utf-8")), content_type=content_type)
        return self._backend.get_durable_url(key)

    def exists(self, key: str) -> bool:
        return self._backend.exists(key)

    def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        return self._backend.get_url(key, expires_in=expires_in)

    def read_text(self, key: str) -> str:
        return self._backend.get(key).decode("utf-8")


def already_described(bucket: Bucket, source_key: str, source_sha: str) -> bool:
    """True when a current described output already exists for this source.

    Resume is content-addressed: the manifest records the source's SHA-256, so
    replacing the source (same key, new bytes) correctly forces a re-describe
    while an unchanged source is skipped.
    """
    import json

    keys = derive_output_keys(source_key)
    if not bucket.exists(keys.manifest):
        return False
    try:
        manifest = json.loads(bucket.read_text(keys.manifest))
    except Exception:  # noqa: BLE001 — a corrupt manifest means describe again
        return False
    recorded = manifest.get("run", {}).get("metadata", {}).get("source_sha256")
    return recorded == source_sha

"""Storage helpers: key derivation and output classification (no network)."""

from __future__ import annotations

from overtone.storage import (
    derive_output_keys,
    is_describable_video,
    is_overtone_output,
)


class TestDeriveOutputKeys:
    def test_outputs_sit_beside_the_source(self):
        keys = derive_output_keys("lectures/week3/lecture.mp4")
        assert keys.described_video == "lectures/week3/lecture.described.mp4"
        assert keys.vtt == "lectures/week3/lecture.ad.vtt"
        assert keys.transcript == "lectures/week3/lecture.ad-transcript.txt"
        assert keys.manifest == "lectures/week3/lecture.ovtmanifest.json"

    def test_handles_a_key_at_the_root(self):
        keys = derive_output_keys("clip.mov")
        assert keys.described_video == "clip.described.mp4"

    def test_all_lists_every_output(self):
        keys = derive_output_keys("a/b.mp4")
        assert len(keys.all()) == 4


class TestClassification:
    def test_common_video_extensions_are_describable(self):
        for key in ("a.mp4", "a.mov", "deep/path/b.webm", "C.MKV"):
            assert is_describable_video(key), key

    def test_non_video_is_not_describable(self):
        for key in ("notes.pdf", "thumb.jpg", "data.json", "readme.txt"):
            assert not is_describable_video(key), key

    def test_overtone_outputs_are_recognized(self):
        keys = derive_output_keys("lectures/x.mp4")
        for k in keys.all():
            assert is_overtone_output(k), k

    def test_described_output_is_never_treated_as_source(self):
        # The critical resume invariant: a re-scan must not re-describe its own
        # described master (which is itself an .mp4).
        assert is_describable_video("lectures/x.mp4") is True
        assert is_describable_video("lectures/x.described.mp4") is False

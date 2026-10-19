"""
Golden-file regression tests for the default SRT/VTT output.

The files in ``tests/golden/`` were generated from the implementation as it was before
stable caption ids / revisions were introduced (Phase 0) and reviewed by eye. They are
compared byte for byte, so any change to the default export format fails here.
To change the format on purpose, regenerate the files and review the diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import SegmentationConfig
from app.services import subtitles
from app.services.captions import Caption, build_captions
from app.services.transcription import RawSegment, Word
from tests.conftest import upload, wait_for

GOLDEN = Path(__file__).parent / "golden"

# Edge cases: multi-line, rounding at a minute boundary, hour boundary, markup and the
# SRT/VTT arrow in text, Unicode, blank/padded lines inside a cue.
DEFAULT_CUES = [
    Caption(1, 0.0, 1.5, "Hello, world."),
    Caption(2, 1.5, 3.25, "Line one\nLine two"),
    Caption(3, 59.9996, 61.0004, "Rounding edge"),
    Caption(4, 3599.999, 3600.0005, "Hour boundary & <b>markup</b> --> arrow"),
    Caption(5, 3725.5, 3726.0, "Ünïcödé — 日本語 ✓"),
    Caption(6, 7200.0, 7201.25, "  padded  \n\nafter blank line\n"),
]


def _words(text: str, start: float, spw: float = 0.4) -> list:
    out, t = [], start
    for w in text.split():
        out.append(Word(text=" " + w, start=t, end=t + spw * 0.9))
        t += spw
    return out


def segmented_captions() -> list:
    """Fixed word timings → the real segmentation rules (pause, sentence end, length)."""
    segs = []
    for text, start in [
        ("Welcome to CaptionForge. This short recording is used to test automatic speech "
         "recognition, and it keeps going for a while so that it must be split.", 0.0),
        ("After a long pause the speaker continues here.", 14.0),
    ]:
        words = _words(text, start)
        segs.append(RawSegment(start=words[0].start, end=words[-1].end, text=text, words=words))
    return build_captions(segs, SegmentationConfig())


def golden(name: str) -> bytes:
    return (GOLDEN / name).read_bytes()


def rendered(captions, fmt: str) -> bytes:
    return subtitles.render(captions, fmt).encode("utf-8")


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_default_cues_match_golden(fmt):
    assert rendered(DEFAULT_CUES, fmt) == golden(f"default_cues.{fmt}")


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_segmented_captions_match_golden(fmt):
    assert rendered(segmented_captions(), fmt) == golden(f"segmented.{fmt}")


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_written_files_match_golden(tmp_path, fmt):
    path = tmp_path / f"out.{fmt}"
    subtitles.write_subtitle(DEFAULT_CUES, fmt, str(path))
    assert path.read_bytes() == golden(f"default_cues.{fmt}")      # LF newlines, no BOM


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_empty_exports_match_golden(fmt):
    assert rendered([], fmt) == golden(f"empty.{fmt}")


# ── Through the HTTP API (real FFmpeg extraction, fake Whisper) ──────────────

# A payload as sent by an older client: no ids, no base_revision. Deliberately unsorted.
OLD_CLIENT_EDITS = {"segments": [
    {"start": 3.0, "end": 4.0, "text": "Zweiter Satz — ünïcode"},
    {"start": 0.5, "end": 2.0, "text": "First line\nsecond line"},
    {"start": 5.0, "end": 6.5, "text": "a --> b & <c>"},
]}


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_api_default_and_edited_downloads_match_golden(make_client, media, fmt):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    assert c.get(f"/api/download/{job_id}?format={fmt}").content == golden(f"api_default.{fmt}")

    r = c.put(f"/api/result/{job_id}/segments", json=OLD_CLIENT_EDITS)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/download/{job_id}?format={fmt}").content == golden(f"api_edited.{fmt}")

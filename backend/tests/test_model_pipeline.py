"""
REAL ML tests: load faster-whisper (WHISPER_MODEL, default "base") and run the
full pipeline on real audio. Marked ``model``; deselect with ``-m "not model"``.

Acceptance criteria (exact transcript equality is not expected):
* word error rate vs. the known script <= 20 %  (base model on clear TTS speech)
* language detected as English
* cue timings valid, monotonic, inside the media duration
* first cue starts within 1 s of the speech start
* no cue is placed inside the 20 s silence of the long clip
* silent / tone-only / very short audio completes without error
"""

from __future__ import annotations

import os
import re
import time

import pytest

from app.config import load_settings
from app.pipeline import run_pipeline
from app.services.transcription import ModelManager
from tests.conftest import SPEECH_SCRIPT, upload, wait_for

pytestmark = pytest.mark.model


def normalise(text: str) -> list[str]:
    text = text.lower().replace("captionforge", "caption forge")
    return re.sub(r"[^\w\s']", " ", text).split()


def wer(ref: str, hyp: str) -> float:
    r, h = normalise(ref), normalise(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


@pytest.fixture(scope="module")
def real_model():
    s = load_settings({"WHISPER_MODEL": os.environ.get("WHISPER_MODEL", "base"),
                       "WHISPER_DEVICE": os.environ.get("WHISPER_DEVICE", "cpu")})
    mm = ModelManager(s.whisper_model, s.whisper_device, s.whisper_compute_type)
    t0 = time.time()
    try:
        mm.get()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"BLOCKED: Whisper model unavailable: {exc}")
    print(f"\n[model] {s.whisper_model} loaded on {mm.active_device} in {time.time() - t0:.1f}s")
    return mm


@pytest.fixture
def run(real_model, settings, tmp_path):
    def _run(path, **kw):
        work = tmp_path / f"work{time.time_ns()}"
        work.mkdir()
        t0 = time.time()
        res = run_pipeline(str(path), str(work), settings, real_model, **kw)
        elapsed = time.time() - t0
        print(f"[model] {os.path.basename(path)}: {res.duration:.1f}s audio in {elapsed:.1f}s "
              f"(RTF {elapsed / max(res.duration, 0.01):.2f}), {len(res.captions)} cues, "
              f"lang={res.language}")
        return res
    return _run


def assert_valid_cues(res):
    prev = 0.0
    for c in res.captions:
        assert 0 <= c.start < c.end <= res.duration + 1.0
        assert c.start >= prev
        prev = c.end


@pytest.mark.parametrize("attr", ["speech_wav", "speech_mp3", "stereo_m4a", "video_mp4"])
def test_clear_speech_accuracy(run, media, attr):
    res = run(getattr(media, attr))
    error_rate = wer(SPEECH_SCRIPT, res.transcript)
    print(f"[model] WER={error_rate:.1%} :: {res.transcript}")
    assert res.language == "en"
    assert error_rate <= 0.20
    assert res.captions and res.captions[0].start < 1.0
    assert_valid_cues(res)
    joined = " ".join(c.text.replace("\n", " ") for c in res.captions)
    assert wer(SPEECH_SCRIPT, joined) <= 0.20     # captions carry the whole transcript


def test_explicit_language(run, media):
    res = run(media.speech_wav, language="en")
    assert res.language == "en" and wer(SPEECH_SCRIPT, res.transcript) <= 0.20


def test_long_clip_with_silence_gap(run, media):
    res = run(media.long_wav)
    assert res.duration == pytest.approx(44.3, abs=0.6)
    assert_valid_cues(res)
    # 12.2 s speech | 20 s silence | 12.2 s speech: nothing should be shown mid-silence
    for c in res.captions:
        assert not (14.0 < c.start < 31.0), c
    second_half = [c for c in res.captions if c.start >= 31.0]
    assert second_half and second_half[0].start < 33.5
    assert wer(SPEECH_SCRIPT + " " + SPEECH_SCRIPT, res.transcript) <= 0.20


@pytest.mark.parametrize("attr", ["silence_wav", "sine_wav", "short_wav"])
def test_non_speech_and_tiny_clips_do_not_crash(run, media, attr):
    res = run(getattr(media, attr))
    assert_valid_cues(res)
    if attr != "short_wav":
        assert res.captions == []


def test_api_with_real_model(real_model, settings, media):
    """Real model through the HTTP layer (TestClient)."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(settings, real_model)) as c:
        r = upload(c, media.video_mp4)
        assert r.status_code == 202
        res = wait_for(c, r.json()["job_id"], timeout=300)
        assert res["status"] == "done" and wer(SPEECH_SCRIPT, res["transcript"]) <= 0.20
        assert c.get(f"/api/download/{res['job_id']}?format=vtt").text.startswith("WEBVTT")


def test_repeated_cuda_attempts_never_hang():
    """Regression: a 2nd CUDA init after a failed one used to hang CTranslate2 forever."""
    import threading
    from app.errors import ModelUnavailableError
    outcomes = []

    def attempt():
        for _ in range(3):
            try:
                ModelManager("base", "cuda", "default").get()
                outcomes.append("ok")
            except ModelUnavailableError:
                outcomes.append("unavailable")

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(120)
    assert not t.is_alive(), "CUDA model loading hung"
    assert len(outcomes) == 3 and len(set(outcomes)) == 1
    mm = ModelManager("base", "auto", "default")
    mm.get()
    assert mm.active_device == ("cuda" if outcomes[0] == "ok" else "cpu")


def test_cancel_real_job_cleans_up(real_model, settings, media, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import ffmpeg
    long = tmp_path / "loop.wav"
    ffmpeg("-stream_loop", "9", "-i", str(media.speech_wav), str(long))   # ~2 min
    with TestClient(create_app(settings, real_model)) as c:
        job_id = upload(c, long).json()["job_id"]
        deadline = time.time() + 120
        while c.get(f"/api/status/{job_id}").json()["status"] != "processing":
            assert time.time() < deadline
            time.sleep(0.1)
        assert c.delete(f"/api/jobs/{job_id}").status_code == 204
        assert c.get(f"/api/status/{job_id}").status_code == 404
        while (settings.upload_dir / job_id).exists():      # stops at the next segment
            assert time.time() < deadline, "upload not cleaned after cancel"
            time.sleep(0.2)
        assert not (settings.output_dir / job_id).exists()
        # worker is free again
        res = wait_for(c, upload(c, media.speech_wav).json()["job_id"])
        assert res["status"] == "done"

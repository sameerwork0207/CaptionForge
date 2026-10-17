"""
Additional component and integration coverage added during the verification
pass (see docs/VERIFICATION_PLAN.md §3), including regression tests for the
defects found in that pass (D1–D8, see docs/VERIFICATION_RESULTS.md).

MOCKED where noted: ``FakeWhisper`` replaces only the neural network; FFmpeg,
validation, segmentation, export, the job queue and HTTP stack are real.
"""

from __future__ import annotations

import json
import struct
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.errors import MediaError
from app.jobs import JobManager
from app.pipeline import run_pipeline
from app.services import audio
from tests.conftest import FakeModelManager, FakeWhisper, ffmpeg, upload, wait_for
from tests.validators import validate_srt, validate_vtt

LEAK_MARKERS = ("Traceback", "ffmpeg version", "C:\\", "/tmp/", "site-packages", "File \"")


def assert_no_leak(body: dict):
    text = json.dumps(body)
    for marker in LEAK_MARKERS:
        assert marker not in text, f"response leaks {marker!r}: {text[:300]}"


def write_wav(path: Path, seconds: float, rate: int = 16000, fmt_tag: int = 1):
    n = int(rate * seconds)
    data = b"\x00\x00" * n
    fmt = struct.pack("<HHIIHH", fmt_tag, 1, rate, rate * 2, 2, 16)
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
                     + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                     + b"data" + struct.pack("<I", len(data)) + data)
    return path


@pytest.fixture(scope="module")
def extra_media(tmp_path_factory, media):
    d = tmp_path_factory.mktemp("extra")
    m = SimpleNamespace()
    m.speech_8k = d / "speech8k.wav"
    ffmpeg("-i", str(media.speech_wav), "-ar", "8000", str(m.speech_8k))
    m.speech_48k_stereo = d / "speech48k.flac"
    ffmpeg("-i", str(media.speech_wav), "-ar", "48000", "-ac", "2", str(m.speech_48k_stereo))
    m.mp3_named_wav = d / "really_mp3.wav"
    m.mp3_named_wav.write_bytes(media.speech_mp3.read_bytes())
    m.bad_codec = write_wav(d / "badcodec.wav", 1.0, fmt_tag=0x9999)
    m.silent_video = d / "silent_track.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i",
           "anullsrc=r=16000:cl=mono", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(m.silent_video))
    m.ogg = d / "speech.ogg"
    ffmpeg("-i", str(media.speech_wav), "-c:a", "libvorbis", str(m.ogg))
    m.opus = d / "speech.opus"
    ffmpeg("-i", str(media.speech_wav), "-c:a", "libopus", str(m.opus))
    m.aac = d / "speech.aac"
    ffmpeg("-i", str(media.speech_wav), "-c:a", "aac", str(m.aac))
    m.mkv = d / "speech.mkv"
    ffmpeg("-i", str(media.video_mp4), "-c", "copy", str(m.mkv))
    m.mov = d / "speech.mov"
    ffmpeg("-i", str(media.video_mp4), "-c", "copy", str(m.mov))
    return m


# ── Media component ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("attr", ["speech_8k", "speech_48k_stereo", "mp3_named_wav", "ogg",
                                  "opus", "aac", "mkv", "mov"])
def test_more_formats_extract_to_16k_mono(extra_media, tmp_path, attr):
    src = getattr(extra_media, attr)
    info = audio.probe_media(str(src))
    audio.validate_media(info, 3600)
    out = tmp_path / "o.wav"
    audio.extract_audio(str(src), str(out))
    with wave.open(str(out)) as wf:
        assert (wf.getframerate(), wf.getnchannels()) == (16000, 1)
    assert audio.wav_duration(str(out)) == pytest.approx(12.2, abs=0.5)


def test_undecodable_codec_rejected_at_probe(extra_media):
    """Regression D5: previously accepted (202) and failed later in the job."""
    info = audio.probe_media(str(extra_media.bad_codec))
    with pytest.raises(MediaError) as ei:
        audio.validate_media(info, 3600)
    assert ei.value.code == "unsupported_codec"


def test_undecodable_codec_extraction_still_fails_cleanly(extra_media, tmp_path):
    with pytest.raises(MediaError) as ei:
        audio.extract_audio(str(extra_media.bad_codec), str(tmp_path / "o.wav"))
    assert ei.value.code == "decode_failed"


def test_ffmpeg_nonzero_exit_is_mapped(media, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, b"", b"secret internal path C:\\x\\y"))
    with pytest.raises(MediaError) as ei:
        audio.extract_audio(str(media.speech_wav), str(tmp_path / "o.wav"))
    assert ei.value.code == "decode_failed" and "C:\\" not in ei.value.message


def test_pipeline_enforces_duration_when_container_omits_it(media, settings, tmp_path):
    s = replace(settings, max_duration_seconds=5)
    with pytest.raises(MediaError) as ei:
        run_pipeline(str(media.speech_wav), str(tmp_path), s, FakeModelManager())
    assert ei.value.code == "media_too_long"


# ── Upload API: format coverage, boundaries, malformed requests ─────────────

@pytest.mark.parametrize("attr", ["speech_8k", "speech_48k_stereo", "mp3_named_wav", "ogg",
                                  "opus", "aac", "mkv", "mov"])
def test_api_accepts_all_supported_formats(make_client, extra_media, attr):
    c = make_client()
    r = upload(c, getattr(extra_media, attr))
    assert r.status_code == 202, r.text
    res = wait_for(c, r.json()["job_id"])
    assert res["status"] == "done" and validate_srt(res["srt_content"])


def test_api_rejects_unknown_codec_at_upload(make_client, extra_media, settings):
    r = upload(make_client(), extra_media.bad_codec)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_codec"
    assert_no_leak(r.json())
    assert list(settings.upload_dir.iterdir()) == []


def test_silent_audio_track_in_video_completes_with_no_captions(make_client, extra_media):
    c = make_client(FakeModelManager(FakeWhisper(text="")))
    res = wait_for(c, upload(c, extra_media.silent_video).json()["job_id"])
    assert res["status"] == "done" and res["segments"] == [] and res["has_video"] is True


@pytest.mark.parametrize("delta,expected", [(-1, 202), (0, 202), (1, 413)])
def test_upload_size_boundary(make_client, settings, tmp_path, delta, expected):
    limit = 1024 * 1024
    c = make_client(settings_override=replace(settings, max_upload_mb=1))
    size = limit + delta
    p = tmp_path / "b.wav"
    write_wav(p, (size - 44) / 32000)
    with open(p, "ab") as f:          # pad to the exact byte size (trailing bytes are ignored)
        f.write(b"\x00" * (size - p.stat().st_size))
    assert p.stat().st_size == size
    r = upload(c, p)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("seconds,expected", [(9.5, 202), (10.0, 202), (10.5, 422)])
def test_duration_boundary(make_client, settings, tmp_path, seconds, expected):
    c = make_client(settings_override=replace(settings, max_duration_seconds=10))
    r = upload(c, write_wav(tmp_path / "d.wav", seconds))
    assert r.status_code == expected, r.text
    if expected == 202:
        assert wait_for(c, r.json()["job_id"])["status"] == "done"
    else:
        assert r.json()["code"] == "media_too_long"


@pytest.mark.parametrize("kwargs", [
    {"json": {"file": "abc"}},                                                  # wrong content type
    {"data": {"file": "not-a-file"}},                                           # text field
    {"content": b"--xx\r\ngarbage", "headers": {"content-type": "multipart/form-data; boundary=xx"}},
    {"content": b"", "headers": {"content-type": "multipart/form-data"}},       # no boundary
])
def test_malformed_upload_requests(make_client, kwargs):
    r = make_client().post("/api/transcribe", **kwargs)
    assert r.status_code in (400, 422), (r.status_code, r.text)
    assert_no_leak(r.json())


@pytest.mark.parametrize("name", ["../../evil.wav", "..\\..\\evil.wav", "C:\\fakepath\\x.wav",
                                  "spa ces (1) [final] ünïcode 日本.wav", "a" * 400 + ".wav",
                                  "nul\x00byte.wav", "<script>.wav"])
def test_hostile_filenames(make_client, media, settings, name):
    c = make_client()
    r = upload(c, media.speech_wav, name=name)
    assert r.status_code == 202, r.text
    job = wait_for(c, r.json()["job_id"])
    assert "/" not in job["filename"] and "\\" not in job["filename"] and "<" not in job["filename"]
    assert len(job["filename"]) <= 200
    dl = c.get(f"/api/download/{job['job_id']}")
    assert dl.status_code == 200
    # nothing written outside the configured directories
    assert not (settings.upload_dir.parent.parent / "evil.wav").exists()
    assert {p.name for p in settings.output_dir.iterdir()} == {job["job_id"]}


# ── Malformed model output (MOCKED model) ────────────────────────────────────

class OddWhisper(FakeWhisper):
    """Returns structurally odd but possible model output."""

    def transcribe(self, audio, **kw):
        nan = float("nan")
        segs = [
            SimpleNamespace(start=0.0, end=1.0, text=" Hello there.", words=None),
            SimpleNamespace(start=1.0, end=1.5, text=" ...", words=[]),
            SimpleNamespace(start=2.0, end=3.0, text=" bad times", words=[
                SimpleNamespace(word=" bad", start=nan, end=nan),
                SimpleNamespace(word=" times", start=-4.0, end=-3.0)]),
            SimpleNamespace(start=3.0, end=3.0, text=" zero", words=[
                SimpleNamespace(word=" zero", start=3.0, end=3.0)]),
            SimpleNamespace(start=2.5, end=4.0, text=" backwards", words=[
                SimpleNamespace(word=" backwards", start=2.5, end=4.0)]),
            SimpleNamespace(start=5.0, end=6.0, text="   ", words=[]),
        ]
        return iter(segs), SimpleNamespace(language="en", language_probability=0.5)


def test_odd_model_output_still_yields_valid_files(make_client, media):
    c = make_client(FakeModelManager(OddWhisper()))
    res = wait_for(c, upload(c, media.speech_wav).json()["job_id"])
    assert res["status"] == "done", res
    words = " ".join(s["text"].replace("\n", " ") for s in res["segments"]).split()
    assert words == ["Hello", "there.", "bad", "times", "zero", "backwards"]
    validate_srt(c.get(f"/api/download/{res['job_id']}").text)
    validate_vtt(c.get(f"/api/download/{res['job_id']}?format=vtt").text)


# ── Edit API ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    '{"segments":[{"start":0,"end":Infinity,"text":"x"}]}',
    '{"segments":[{"start":NaN,"end":1,"text":"x"}]}',
    '{"segments":[{"start":0,"end":1e308,"text":"x"}]}',
    '{"segments":[{"start":"0","end":"abc","text":"x"}]}',
    '{"segments":[{"start":0,"end":1,"text":"' + "x" * 1001 + '"}]}',
    '{"segments":"nope"}', '[]', 'not json',
])
def test_edit_rejects_invalid_bodies(make_client, media, body):
    """Regression D1: Infinity used to produce HTTP 500."""
    c = make_client()
    jid = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, jid)
    r = c.put(f"/api/result/{jid}/segments", content=body,
              headers={"content-type": "application/json"})
    assert r.status_code == 422, (r.status_code, r.text)
    assert_no_leak(r.json())
    validate_srt(c.get(f"/api/download/{jid}").text)       # previous captions intact


def test_concurrent_edits_are_atomic(make_client, media, settings):
    c = make_client()
    jid = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, jid)
    bodies = [{"segments": [{"start": 0, "end": 1, "text": f"version {i} " + "w " * 50}]}
              for i in range(12)]
    codes = []
    threads = [threading.Thread(target=lambda b=b: codes.append(
        c.put(f"/api/result/{jid}/segments", json=b).status_code)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200] * 12
    final = c.get(f"/api/result/{jid}").json()["segments"][0]["text"]
    srt_text = validate_srt(c.get(f"/api/download/{jid}").text)[0][2]
    vtt_text = validate_vtt(c.get(f"/api/download/{jid}?format=vtt").text)[0][2]
    assert final == srt_text == vtt_text                 # files and record agree
    stored = json.loads((settings.output_dir / jid / "job.json").read_text(encoding="utf-8"))
    assert stored["segments"][0]["text"] == final


def test_edit_after_delete_is_404_not_500(make_client, media):
    """Regression D4: a job deleted between lookup and save raised KeyError (500)."""
    c = make_client()
    jid = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, jid)
    jobs = c.app.state.jobs
    original_get = jobs.get

    def get_then_delete(job_id):
        rec = original_get(job_id)
        jobs.delete(job_id)                    # simulate a concurrent DELETE
        return rec
    jobs.get = get_then_delete
    r = c.put(f"/api/result/{jid}/segments", json={"segments": [{"start": 0, "end": 1, "text": "x"}]})
    jobs.get = original_get
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_same_filename_jobs_are_independent(make_client, media, settings):
    c = make_client()
    ids = [upload(c, media.speech_wav, name="same.wav").json()["job_id"] for _ in range(3)]
    for jid in ids:
        wait_for(c, jid)
    c.put(f"/api/result/{ids[0]}/segments", json={"segments": [{"start": 0, "end": 1, "text": "only first"}]})
    assert c.get(f"/api/result/{ids[1]}").json()["segments"][0]["text"] != "only first"
    assert c.delete(f"/api/jobs/{ids[2]}").status_code == 204
    assert c.get(f"/api/download/{ids[1]}").status_code == 200
    assert sorted(p.name for p in settings.output_dir.iterdir()) == sorted(ids[:2])


# ── Persistence / recovery ───────────────────────────────────────────────────

BAD_RECORDS = {
    "missing-status": {"job_id": "{id}"},
    "bad-status": {"job_id": "{id}", "status": "exploded"},
    "id-mismatch": {"job_id": "aaaaaaaa-0000-0000-0000-00000000ffff", "status": "done"},
    "not-a-dict": ["list"],
    "bad-segments": {"job_id": "{id}", "status": "done", "segments": [{"start": "x"}]},
    "bad-error": {"job_id": "{id}", "status": "error", "error": "string"},
}


def test_malformed_job_records_do_not_break_startup(settings, tmp_path):
    """Regression D2: a record without 'status' crashed startup with KeyError."""
    good = "aaaaaaaa-0000-0000-0000-000000000000"
    (settings.output_dir / good).mkdir(parents=True)
    (settings.output_dir / good / "job.json").write_text(json.dumps(
        {"job_id": good, "status": "done", "finished_at": "not a date", "segments": [],
         "filename": "ok.wav"}), encoding="utf-8")
    for i, rec in enumerate(BAD_RECORDS.values(), start=1):
        jid = f"aaaaaaaa-0000-0000-0000-{i:012d}"
        (settings.output_dir / jid).mkdir()
        payload = json.dumps(rec).replace("{id}", jid)
        (settings.output_dir / jid / "job.json").write_text(payload, encoding="utf-8")
    garbage = "aaaaaaaa-0000-0000-0000-000000000099"
    (settings.output_dir / garbage).mkdir()
    (settings.output_dir / garbage / "job.json").write_bytes(b"\xff\xfe{{{not json")

    jm = JobManager(settings, FakeModelManager())
    jm.start()
    try:
        assert jm.get(good)["status"] == "done"
        for i in range(1, len(BAD_RECORDS) + 1):
            assert jm.get(f"aaaaaaaa-0000-0000-0000-{i:012d}") is None
        assert jm.get("aaaaaaaa-0000-0000-0000-00000000ffff") is None
        assert jm.purge_expired() == 0           # unparsable finished_at reset, not crash
    finally:
        jm.stop()


def test_graceful_shutdown_marks_jobs_interrupted(settings, media):
    """Regression D3: graceful shutdown used to DELETE in-flight and pending jobs."""
    started = threading.Event()
    ran = []

    def pipeline(input_path, work_dir, s, mm, *, should_cancel, **kw):
        ran.append(input_path)
        started.set()
        while not should_cancel():
            time.sleep(0.01)
        from app.errors import JobCancelledError
        raise JobCancelledError("stop")

    jm = JobManager(settings, None, pipeline)
    jm.start()
    ids = []
    for _ in range(3):
        jid = jm.new_job_id()
        d = jm.upload_dir(jid)
        d.mkdir(parents=True)
        (d / "input.wav").write_bytes(b"x")
        jm.submit(jid, str(d / "input.wav"), "a.wav", None, False)
        ids.append(jid)
    assert started.wait(5)
    jm.stop()
    assert len(ran) == 1                         # pending jobs were not started
    assert jm.get(ids[0])["error"]["code"] == "interrupted"

    jm2 = JobManager(settings, None, pipeline)
    jm2.start()
    try:
        for jid in ids:
            rec = jm2.get(jid)
            assert rec["status"] == "error" and rec["error"]["code"] == "interrupted"
        assert list(settings.upload_dir.iterdir()) == []
    finally:
        jm2.stop()


# ── CORS ─────────────────────────────────────────────────────────────────────

def test_cors_rejects_unknown_origin(make_client):
    r = make_client().options("/api/health", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_download_exposes_content_disposition_to_frontend(make_client, media):
    c = make_client()
    jid = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, jid)
    r = c.get(f"/api/download/{jid}", headers={"Origin": "http://localhost:3000"})
    assert "content-disposition" in r.headers.get("access-control-expose-headers", "").lower()

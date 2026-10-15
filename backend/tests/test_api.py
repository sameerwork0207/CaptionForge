"""
API integration tests through FastAPI's TestClient.

MOCKED: the Whisper network is replaced by ``FakeWhisper``. Everything else
(upload handling, ffprobe validation, ffmpeg extraction, segmentation,
export, job queue, persistence) runs for real.
"""

import threading
from dataclasses import replace

import pytest

from app.errors import ModelUnavailableError
from tests.conftest import FakeModelManager, FakeWhisper, upload, wait_for
from tests.test_subtitles import parse_srt, parse_vtt


def disposition_filename(header: str) -> str:
    """Filename from Content-Disposition, accepting plain or RFC 5987 (filename*=) form."""
    from urllib.parse import unquote
    assert header.startswith("attachment;")
    if "filename*=utf-8''" in header:
        return unquote(header.split("filename*=utf-8''", 1)[1].split(";")[0])
    return header.split('filename="', 1)[1].split('"')[0]


def test_health_and_config(make_client):
    c = make_client()
    h = c.get("/api/health").json()
    assert h["status"] == "ok" and h["ffmpeg"] is True
    cfg = c.get("/api/config").json()
    assert ".mp4" in cfg["supported_extensions"] and "en" in cfg["languages"]
    assert cfg["export_formats"] == ["srt", "vtt"]


@pytest.mark.parametrize("attr", ["speech_wav", "speech_mp3", "stereo_m4a", "video_mp4",
                                  "video_webm"])
def test_full_flow_for_supported_inputs(make_client, media, attr):
    c = make_client()
    r = upload(c, getattr(media, attr))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    res = wait_for(c, body["job_id"])
    assert res["status"] == "done", res
    assert res["language"] == "en" and res["duration"] == pytest.approx(12.2, abs=0.4)
    assert res["has_video"] is attr.startswith("video")
    assert len(res["segments"]) >= 3
    assert parse_srt(res["srt_content"])


def test_status_progress_and_downloads(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav, name="Lecture 1 (draft).wav").json()["job_id"]
    wait_for(c, job_id)
    st = c.get(f"/api/status/{job_id}").json()
    assert st["status"] == "done" and st["progress"] == 1.0 and st["filename"] == "Lecture 1 (draft).wav"

    srt = c.get(f"/api/download/{job_id}")
    assert srt.status_code == 200
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert disposition_filename(srt.headers["content-disposition"]) == "Lecture_1_(draft).srt"
    assert len(parse_srt(srt.text)) >= 3

    vtt = c.get(f"/api/download/{job_id}?format=vtt")
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert len(parse_vtt(vtt.text)) == len(parse_srt(srt.text))

    assert c.get(f"/api/download/{job_id}?format=ass").status_code == 422


def test_language_is_passed_to_model(make_client, media):
    fake = FakeWhisper()
    c = make_client(FakeModelManager(fake))
    job_id = upload(c, media.speech_wav, language="fr").json()["job_id"]
    wait_for(c, job_id)
    assert fake.calls[0][1]["language"] == "fr"
    assert fake.calls[0][1]["word_timestamps"] is True


def test_invalid_language(make_client, media):
    r = upload(make_client(), media.speech_wav, language="klingon")
    assert r.status_code == 422 and r.json()["code"] == "invalid_language"


@pytest.mark.parametrize("attr,name,status,code", [
    ("empty", None, 422, "empty_file"),
    ("corrupt", None, 422, "corrupt_media"),
    ("text_as_wav", None, 422, "corrupt_media"),
    ("video_no_audio", None, 422, "no_audio_stream"),
    ("speech_wav", "notes.txt", 415, "unsupported_format"),
    ("speech_wav", "noext", 415, "unsupported_format"),
])
def test_rejected_uploads(make_client, media, settings, attr, name, status, code):
    c = make_client()
    r = upload(c, getattr(media, attr), name=name)
    assert r.status_code == status, r.text
    body = r.json()
    if code:
        assert body["code"] == code
    assert body["detail"] and "\\" not in body["detail"] and "ffmpeg version" not in body["detail"]
    assert list(settings.upload_dir.iterdir()) == []          # nothing left behind


def test_truncated_mp4_fails_gracefully(make_client, media):
    """A truncated file is rejected at upload or fails as a job — never a 500."""
    c = make_client()
    r = upload(c, media.truncated_mp4)
    assert r.status_code in (202, 422)
    if r.status_code == 202:
        assert wait_for(c, r.json()["job_id"])["status"] == "error"


def test_oversized_upload(make_client, media, settings):
    c = make_client(settings_override=replace(settings, max_upload_mb=1))
    big = settings.upload_dir.parent / "big.wav"
    big.write_bytes(media.speech_wav.read_bytes() * 6)   # ~2.3 MB
    r = upload(c, big)
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


def test_copy_limit_without_content_length(tmp_path):
    import io
    from app.api.routes import _copy_limited
    from app.errors import CaptionForgeError
    with pytest.raises(CaptionForgeError) as ei:
        _copy_limited(io.BytesIO(b"x" * 3000), str(tmp_path / "f"), limit=1000)
    assert ei.value.http_status == 413


def test_missing_file_field(make_client):
    r = make_client().post("/api/transcribe", data={"language": "en"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


def test_silent_and_short_audio_complete_with_no_captions(make_client, media):
    c = make_client(FakeModelManager(FakeWhisper(text="")))
    for f in (media.silence_wav, media.short_wav):
        res = wait_for(c, upload(c, f).json()["job_id"])
        assert res["status"] == "done" and res["segments"] == [] and res["srt_content"] == ""
        dl = c.get(f"/api/download/{res['job_id']}?format=vtt")
        assert dl.status_code == 200 and dl.text == "WEBVTT\n"


@pytest.mark.parametrize("job_id", ["nope", "00000000-0000-0000-0000-000000000000",
                                    "..%2F..%2Fjob"])
def test_unknown_jobs_404(make_client, job_id):
    c = make_client()
    for method, url in [("get", f"/api/status/{job_id}"), ("get", f"/api/result/{job_id}"),
                        ("get", f"/api/download/{job_id}"), ("delete", f"/api/jobs/{job_id}")]:
        r = getattr(c, method)(url)
        assert r.status_code == 404, (url, r.status_code)


def test_result_not_ready_returns_409_for_download(make_client, media):
    gate = threading.Event()

    class Slow(FakeWhisper):
        def transcribe(self, audio, **kw):
            gate.wait(10)
            return super().transcribe(audio, **kw)

    c = make_client(FakeModelManager(Slow()))
    job_id = upload(c, media.speech_wav).json()["job_id"]
    res = c.get(f"/api/result/{job_id}").json()
    assert res["status"] in ("pending", "processing") and res["segments"] is None
    assert c.get(f"/api/download/{job_id}").status_code == 409
    assert c.put(f"/api/result/{job_id}/segments", json={"segments": []}).status_code == 409
    gate.set()
    assert wait_for(c, job_id)["status"] == "done"


def test_model_load_failure_is_reported(make_client, media):
    mm = FakeModelManager(load_error=ModelUnavailableError("Could not load Whisper model 'x'."))
    c = make_client(mm)
    res = wait_for(c, upload(c, media.speech_wav).json()["job_id"])
    assert res["status"] == "error" and res["error"]["code"] == "model_unavailable"


def test_inference_crash_is_reported_not_500(make_client, media):
    c = make_client(FakeModelManager(FakeWhisper(error=RuntimeError("kaboom"))))
    res = wait_for(c, upload(c, media.speech_wav).json()["job_id"])
    assert res["status"] == "error" and res["error"]["code"] == "transcription_failed"
    # the server keeps working afterwards
    assert c.get("/api/health").status_code == 200


def test_edit_captions(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    edits = {"segments": [{"start": 3.0, "end": 4.0, "text": "Zweiter Satz — ünïcode"},
                          {"start": 0.5, "end": 2.0, "text": "First line\nsecond line"}]}
    r = c.put(f"/api/result/{job_id}/segments", json=edits)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["edited"] is True and [s["index"] for s in body["segments"]] == [1, 2]
    srt = c.get(f"/api/download/{job_id}").text
    cues = parse_srt(srt)
    assert cues[0] == (0.5, 2.0, "First line\nsecond line") and "ünïcode" in cues[1][2]
    # result endpoint reflects the edit
    assert c.get(f"/api/result/{job_id}").json()["segments"][1]["text"].startswith("Zweiter")


@pytest.mark.parametrize("segments,status", [
    ([{"start": 2, "end": 1, "text": "x"}], 422),
    ([{"start": 0, "end": 1, "text": ""}], 422),
    ([{"start": 0, "end": 2, "text": "a"}, {"start": 1, "end": 3, "text": "b"}], 422),
    ([{"start": -1, "end": 1, "text": "x"}], 422),
    ([{"start": 0, "text": "missing end"}], 422),
])
def test_edit_validation(make_client, media, segments, status):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    r = c.put(f"/api/result/{job_id}/segments", json={"segments": segments})
    assert r.status_code == status and r.json()["detail"]


def test_delete_job(make_client, media, settings):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    assert c.delete(f"/api/jobs/{job_id}").status_code == 204
    assert c.get(f"/api/status/{job_id}").status_code == 404
    assert not (settings.output_dir / job_id).exists()
    assert c.delete(f"/api/jobs/{job_id}").status_code == 404


def test_concurrent_uploads_do_not_collide(make_client, media):
    c = make_client(FakeModelManager(FakeWhisper(delay=0.02)))
    results = []

    def go(path):
        results.append(upload(c, path))

    threads = [threading.Thread(target=go, args=(p,))
               for p in (media.speech_wav, media.speech_mp3, media.video_mp4, media.stereo_m4a)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code == 202 for r in results)
    ids = {r.json()["job_id"] for r in results}
    assert len(ids) == 4
    for j in ids:
        res = wait_for(c, j)
        assert res["status"] == "done" and len(res["segments"]) >= 3


def test_repeated_requests_are_idempotent(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    first = c.get(f"/api/download/{job_id}").text
    for _ in range(5):
        assert c.get(f"/api/result/{job_id}").status_code == 200
        assert c.get(f"/api/download/{job_id}").text == first


def test_missing_output_file(make_client, media, settings):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    (settings.output_dir / job_id / "captions.srt").unlink()
    r = c.get(f"/api/download/{job_id}")
    assert r.status_code == 404 and r.json()["code"] == "output_missing"


def test_ffmpeg_missing_returns_503(make_client, media, monkeypatch):
    c = make_client()
    monkeypatch.setenv("PATH", "")
    assert c.get("/api/health").json()["ffmpeg"] is False
    r = upload(c, media.speech_wav)
    assert r.status_code == 503 and r.json()["code"] == "ffmpeg_unavailable"


def test_cors_allows_frontend_origin(make_client):
    r = make_client().options("/api/health", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

"""
Shared fixtures.

Media fixtures are generated with FFmpeg at test time (except
``fixtures/speech_en.wav``, a committed 12 s clip of synthesised English
speech with a known script, see ``SPEECH_SCRIPT``).

``FakeModelManager`` replaces only the neural network: tests that use it still
run real FFmpeg probing/extraction, segmentation, export and job handling.
Tests that load the real Whisper model are marked ``@pytest.mark.model``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures"
SPEECH_WAV = FIXTURES / "speech_en.wav"
SPEECH_SCRIPT = ("Welcome to CaptionForge. This short recording is used to test automatic "
                 "speech recognition. The quick brown fox jumps over the lazy dog. "
                 "Thank you for listening.")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> SimpleNamespace:
    """Generate one set of media files for the whole session."""
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    m = SimpleNamespace(speech_wav=SPEECH_WAV)
    m.sine_wav = d / "sine.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3", "-ar", "16000", "-ac", "1", str(m.sine_wav))
    m.silence_wav = d / "silence.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "4", str(m.silence_wav))
    m.short_wav = d / "short.wav"      # 0.3 s clip
    ffmpeg("-i", str(SPEECH_WAV), "-t", "0.3", str(m.short_wav))
    m.speech_mp3 = d / "speech.mp3"
    ffmpeg("-i", str(SPEECH_WAV), "-c:a", "libmp3lame", "-b:a", "64k", str(m.speech_mp3))
    m.stereo_m4a = d / "stereo.m4a"
    ffmpeg("-i", str(SPEECH_WAV), "-ac", "2", "-ar", "44100", "-c:a", "aac", str(m.stereo_m4a))
    m.video_mp4 = d / "video.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-i", str(SPEECH_WAV),
           "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(m.video_mp4))
    m.video_webm = d / "video.webm"
    ffmpeg("-i", str(m.video_mp4), "-c:v", "libvpx", "-b:v", "100k", "-c:a", "libopus",
           str(m.video_webm))
    m.video_no_audio = d / "silent_video.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "2", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", str(m.video_no_audio))
    m.long_wav = d / "long.wav"        # speech, 20 s silence, speech (~44 s)
    ffmpeg("-i", str(SPEECH_WAV), "-f", "lavfi", "-t", "20", "-i", "anullsrc=r=16000:cl=mono",
           "-i", str(SPEECH_WAV), "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
           "-ar", "16000", "-ac", "1", str(m.long_wav))
    m.empty = d / "empty.mp3"
    m.empty.write_bytes(b"")
    m.corrupt = d / "corrupt.mp4"
    m.corrupt.write_bytes(b"\x00\x01garbage-not-media" * 500)
    m.text_as_wav = d / "notes.wav"
    m.text_as_wav.write_text("this is not audio\n" * 50)
    m.truncated_mp4 = d / "truncated.mp4"
    m.truncated_mp4.write_bytes(m.video_mp4.read_bytes()[:2000])
    return m


@pytest.fixture
def settings(tmp_path, monkeypatch):
    env = {"UPLOAD_DIR": str(tmp_path / "uploads"), "OUTPUT_DIR": str(tmp_path / "outputs"),
           "WHISPER_MODEL": "base", "WHISPER_DEVICE": "cpu", "JOB_RETENTION_HOURS": "24"}
    return load_settings(env)


# ── Fake Whisper model (mocked inference only) ────────────────────────────────

class FakeWhisper:
    """Mimics faster_whisper.WhisperModel.transcribe for a given script."""

    def __init__(self, text: str = SPEECH_SCRIPT, delay: float = 0.0, error: Exception = None,
                 seconds_per_word: float = 0.4):
        self.text, self.delay, self.error, self.spw = text, delay, error, seconds_per_word
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        if self.error:
            raise self.error
        words = self.text.split()

        def gen():
            t = 0.0
            for chunk_start in range(0, len(words), 8):
                chunk = words[chunk_start:chunk_start + 8]
                if self.delay:
                    time.sleep(self.delay)
                ws = []
                for w in chunk:
                    ws.append(SimpleNamespace(word=" " + w, start=t, end=t + self.spw * 0.9))
                    t += self.spw
                yield SimpleNamespace(start=ws[0].start, end=ws[-1].end,
                                      text=" " + " ".join(chunk), words=ws)

        return gen(), SimpleNamespace(language="en", language_probability=0.98)


class FakeModelManager:
    def __init__(self, model=None, load_error: Exception = None):
        self.model = model or FakeWhisper()
        self.load_error = load_error
        self.model_name = "fake"
        self.active_device = "cpu"
        self.loaded = True

    def get(self):
        if self.load_error:
            raise self.load_error
        return self.model


@pytest.fixture
def make_client(settings):
    """Factory: TestClient around a fresh app with the given (fake) model manager."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    clients = []

    def _make(model_manager=None, settings_override=None):
        app = create_app(settings_override or settings, model_manager or FakeModelManager())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def upload(client, path: Path, name: str = None, language: str = None):
    data = {"language": language} if language else None
    with open(path, "rb") as fh:
        return client.post("/api/transcribe", files={"file": (name or path.name, fh)}, data=data)


def wait_for(client, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/status/{job_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] in ("done", "error"):
            return client.get(f"/api/result/{job_id}").json()
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")

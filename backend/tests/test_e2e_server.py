"""
END-TO-END (backend): start a real uvicorn process with the real model and
exercise the HTTP API exactly like the frontend does:

    upload → poll status → result → edit → download SRT/VTT → parse → delete

Also verifies restart behaviour: finished jobs survive a server restart.
Marked ``e2e`` (and ``model``).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import SPEECH_WAV
from tests.test_subtitles import parse_srt, parse_vtt

pytestmark = [pytest.mark.e2e, pytest.mark.model]
BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp: Path):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "UPLOAD_DIR": str(tmp / "uploads"),
                    "OUTPUT_DIR": str(tmp / "outputs"), "WHISPER_DEVICE": "cpu"}
        self.log = tmp / f"server-{self.port}.log"
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=BACKEND, env=self.env, stdout=open(self.log, "w"), stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(self.url + "/api/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        raise AssertionError("server did not start:\n" + self.log.read_text())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(20)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path).start()
    yield s
    s.stop()


def test_full_journey_over_http(server, tmp_path):
    with httpx.Client(base_url=server.url, timeout=30) as c:
        with open(SPEECH_WAV, "rb") as fh:
            r = c.post("/api/transcribe", files={"file": ("Demo clip.wav", fh, "audio/wav")})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        seen, deadline = set(), time.time() + 300
        while time.time() < deadline:
            st = c.get(f"/api/status/{job_id}").json()
            seen.add(st["status"])
            if st["status"] in ("done", "error"):
                break
            time.sleep(0.2)
        assert st["status"] == "done", st
        assert seen <= {"pending", "processing", "done"}

        res = c.get(f"/api/result/{job_id}").json()
        assert res["language"] == "en" and len(res["segments"]) >= 3

        segs = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in res["segments"]]
        segs[0]["text"] = "Welcome to CaptionForge!"
        assert c.put(f"/api/result/{job_id}/segments", json={"segments": segs}).status_code == 200

        srt = c.get(f"/api/download/{job_id}")
        assert srt.status_code == 200 and "attachment" in srt.headers["content-disposition"]
        cues = parse_srt(srt.text)
        assert cues[0][2] == "Welcome to CaptionForge!"
        vtt = parse_vtt(c.get(f"/api/download/{job_id}", params={"format": "vtt"}).text)
        assert len(vtt) == len(cues)

        # Bad input over real HTTP
        bad = c.post("/api/transcribe", files={"file": ("x.mp4", b"not a video", "video/mp4")})
        assert bad.status_code == 422 and bad.json()["code"] == "corrupt_media"

    # Restart: the finished job is still available afterwards.
    server.stop()
    server.start()
    with httpx.Client(base_url=server.url, timeout=30) as c:
        assert c.get(f"/api/status/{job_id}").json()["status"] == "done"
        assert parse_srt(c.get(f"/api/download/{job_id}").text)[0][2] == "Welcome to CaptionForge!"
        assert c.delete(f"/api/jobs/{job_id}").status_code == 204
        assert c.get(f"/api/status/{job_id}").status_code == 404

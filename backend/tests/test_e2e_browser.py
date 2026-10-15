"""
END-TO-END (browser): real backend (uvicorn + real Whisper model) + production
Next.js build, driven through headless Chrome/Edge with Playwright.

    open UI → reject bad file client-side → upload video → progress →
    captions shown → edit + save → download .srt (parse it) → reload resumes
    the job → corrupt upload shows server error → mobile layout has no
    horizontal overflow → backend down shows a banner

Requires: Node/npm with ``frontend/node_modules`` installed, Playwright
(``pip install -r requirements-e2e.txt``) and Chrome or Edge.
Run with:  pytest -m e2e tests/test_e2e_browser.py
Screenshots are written to ``$E2E_SCREENSHOT_DIR`` if set.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import SPEECH_SCRIPT
from tests.test_e2e_server import Server
from tests.test_subtitles import parse_srt

pytestmark = [pytest.mark.e2e, pytest.mark.model]
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
NPX = shutil.which("npx") or shutil.which("npx.cmd")

playwright = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch_browser(p):
    for channel in ("chrome", "msedge", None):
        try:
            return p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        except Exception:  # noqa: BLE001 - try the next available browser
            continue
    pytest.skip("BLOCKED: no Chromium-based browser available for Playwright")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if not NPX or not (FRONTEND / "node_modules").exists():
        pytest.skip("BLOCKED: frontend dependencies not installed (npm install)")
    tmp = tmp_path_factory.mktemp("e2e")
    fe_port = _free_port()
    fe_url = f"http://127.0.0.1:{fe_port}"

    backend = Server(tmp)
    backend.env["CORS_ORIGINS"] = fe_url
    backend.start()

    env = {**os.environ, "NEXT_PUBLIC_API_BASE_URL": backend.url, "NEXT_DIST_DIR": ".next-e2e",
           "NEXT_TELEMETRY_DISABLED": "1"}
    build = subprocess.run([NPX, "next", "build"], cwd=FRONTEND, env=env, capture_output=True,
                           text=True, timeout=600, shell=os.name == "nt")
    assert build.returncode == 0, build.stdout[-3000:] + build.stderr[-3000:]
    fe_log = open(tmp / "frontend.log", "w")
    fe = subprocess.Popen([NPX, "next", "start", "-p", str(fe_port), "-H", "127.0.0.1"],
                          cwd=FRONTEND, env=env, stdout=fe_log, stderr=subprocess.STDOUT,
                          shell=os.name == "nt")
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(fe_url, timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        raise AssertionError("frontend did not start")
    yield {"backend": backend, "fe_url": fe_url, "tmp": tmp}
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(fe.pid), "/T", "/F"], capture_output=True)
    else:
        fe.terminate()
    backend.stop()


def _shot(page, name):
    d = os.environ.get("E2E_SCREENSHOT_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"), full_page=True)


def test_browser_journey(stack, media, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(stack["fe_url"])
        expect(page.get_by_text("Supported:")).to_be_visible(timeout=20_000)
        _shot(page, "01-upload")

        # Client-side validation
        bad = tmp_path / "notes.txt"
        bad.write_text("hello")
        page.get_by_test_id("file-input").set_input_files(str(bad))
        expect(page.locator("p.error")).to_contain_text("not supported")
        expect(page.get_by_role("button", name="Generate captions")).to_be_disabled()

        # Real upload → processing → captions
        page.get_by_test_id("file-input").set_input_files(str(media.video_mp4))
        page.get_by_role("button", name="Generate captions").click()
        expect(page.get_by_test_id("cue-list")).to_be_visible(timeout=180_000)
        cues = page.locator("[data-testid=cue-list] li")
        assert cues.count() >= 3
        first_text = page.get_by_label("Caption 1 text")
        expect(first_text).to_have_value("Welcome to Caption Forge.")
        expect(page.get_by_test_id("result-meta")).to_contain_text("language en")
        assert page.evaluate("document.querySelector('video').textTracks.length") == 1
        _shot(page, "02-editor")

        # Edit + save
        first_text.fill("Welcome to CaptionForge!")
        expect(page.get_by_test_id("save-state")).to_have_text("Unsaved changes")
        page.get_by_label("Caption 2 end").fill("bogus")
        expect(page.get_by_role("button", name="Save changes")).to_be_disabled()
        page.get_by_label("Caption 2 end").fill("00:00:05.900")
        page.get_by_role("button", name="Save changes").click()
        expect(page.get_by_test_id("save-state")).to_contain_text("Saved at")

        # Download .srt and validate it
        with page.expect_download() as dl:
            page.get_by_role("button", name="Download .srt").click()
        path = tmp_path / dl.value.suggested_filename
        dl.value.save_as(path)
        assert path.name == "video.srt"
        parsed = parse_srt(path.read_text(encoding="utf-8"))
        assert parsed[0][2] == "Welcome to CaptionForge!"
        assert parsed[1][1] == pytest.approx(5.9)
        joined = " ".join(c[2].replace("\n", " ") for c in parsed)
        assert "lazy dog" in joined and len(joined) > len(SPEECH_SCRIPT) * 0.8

        # Reload resumes the job from ?job=
        assert "job=" in page.url
        done_job = page.url.split("job=")[1].split("&")[0]
        page.reload()
        expect(page.get_by_label("Caption 1 text")).to_have_value("Welcome to CaptionForge!",
                                                                timeout=20_000)
        expect(page.get_by_text("Media preview is available only")).to_be_visible()

        # Corrupt file: passes client checks, rejected by the server with a clear message
        page.get_by_role("button", name="Caption another file").click()
        page.get_by_test_id("file-input").set_input_files(str(media.corrupt))
        page.get_by_role("button", name="Generate captions").click()
        expect(page.get_by_test_id("error-message")).to_contain_text("could not be read",
                                                                     timeout=20_000)
        _shot(page, "03-error")

        # Mobile layout: no horizontal scrolling on the editor
        mobile = browser.new_context(viewport={"width": 375, "height": 740}, is_mobile=True)
        mp = mobile.new_page()
        mp.goto(stack["fe_url"])
        expect(mp.get_by_text("Supported:")).to_be_visible(timeout=20_000)
        assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _shot(mp, "04-mobile-upload")
        mp.goto(f"{stack['fe_url']}/?job={done_job}")
        expect(mp.get_by_test_id("cue-list")).to_be_visible(timeout=20_000)
        assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _shot(mp, "05-mobile-editor")
        mobile.close()

        # Backend down → banner
        stack["backend"].stop()
        page.goto(stack["fe_url"])
        expect(page.locator(".banner")).to_contain_text("Cannot reach", timeout=20_000)
        browser.close()

"""
FULL-STACK SCENARIOS A–L (see docs/VERIFICATION_PLAN.md).

Everything is real: uvicorn backend with the real faster-whisper model, a
production Next.js build, headless Chrome via Playwright. Browser network
traffic and console errors are recorded and asserted on.

Two stacks are used:
* ``main``: default limits, working model (scenarios A–E, G–K)
* ``constrained``: MAX_UPLOAD_MB=1, MAX_DURATION_SECONDS=10 and a model
  directory that does not exist yet (scenario F: real model-load failure, then
  the model is installed and later jobs succeed; scenario L: limits)

Optional: set LIBRISPEECH_DIR to a folder with LibriSpeech *.flac files to also
run the genuine-human-speech audio and video workflows (A2, B2).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.conftest import ffmpeg
from tests.e2e_support import Stack, Watched, launch_browser, shot
from tests.validators import validate_srt, validate_vtt

pytestmark = [pytest.mark.e2e, pytest.mark.model]
playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

PROCESS_TIMEOUT = 240_000
# Chrome logs every 4xx/5xx fetch as a console error; those are expected in negative scenarios.
HTTP_ERROR_LOG = "Failed to load resource"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def clips(media, tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    c = {"speech_mp3": media.speech_mp3, "video_webm": media.video_webm, "video_mp4": media.video_mp4,
         "silence": media.silence_wav, "empty": media.empty, "corrupt": media.corrupt,
         "text_as_wav": media.text_as_wav, "speech_wav": media.speech_wav}
    c["first_half"] = d / "first.wav"
    ffmpeg("-i", str(media.speech_wav), "-t", "5.9", str(c["first_half"]))
    c["second_half"] = d / "second.wav"
    ffmpeg("-i", str(media.speech_wav), "-ss", "6.4", str(c["second_half"]))
    c["long"] = d / "long two minutes.wav"
    ffmpeg("-stream_loop", "9", "-i", str(media.speech_wav), str(c["long"]))
    c["silent_video"] = d / "silent video.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i",
           "anullsrc=r=16000:cl=mono", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(c["silent_video"]))
    c["notes_txt"] = d / "notes.txt"
    c["notes_txt"].write_text("hello")
    from tests.test_components_extra import write_wav
    c["bad_codec"] = write_wav(d / "badcodec.wav", 1.0, fmt_tag=0x9999)
    for secs in (9.5, 10.0, 10.5):
        p = d / f"dur_{secs}.wav"
        ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-i", str(media.speech_wav),
               "-filter_complex", "[1:a][0:a]concat=n=2:v=0:a=1", "-t", str(secs), "-ar", "16000",
               "-ac", "1", str(p))
        c[f"dur_{secs}"] = p
    for name, size in (("size_below", 1024 * 1024 - 1), ("size_at", 1024 * 1024),
                       ("size_above", 1024 * 1024 + 1)):
        p = d / f"{name}.wav"
        write_wav(p, 2.0)            # short audio (below the duration limit), padded to size
        with open(p, "ab") as f:
            f.write(b"\x00" * (size - p.stat().st_size))
        c[name] = p
    return c


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = launch_browser(p)
        yield b
        b.close()


@pytest.fixture(scope="module")
def main_stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("main"), ".next-e2e-main").start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def constrained_stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("constrained")
    model_dir = tmp / "model-not-installed-yet"
    s = Stack(tmp, ".next-e2e-constrained", {
        "MAX_UPLOAD_MB": "1", "MAX_DURATION_SECONDS": "10",
        "WHISPER_MODEL": model_dir.as_posix()})
    s.model_dir = model_dir
    s.start()
    yield s
    s.stop()


def new_page(browser, stack, **ctx_kwargs):
    ctx = browser.new_context(accept_downloads=True, **ctx_kwargs)
    w = Watched(ctx.new_page(), stack.backend.url)
    w.page.goto(stack.fe_url)
    expect(w.page.get_by_text("Supported:")).to_be_visible(timeout=30_000)
    return w


def submit(w: Watched, path: Path, language: str = None):
    w.page.get_by_test_id("file-input").set_input_files(str(path))
    if language:
        w.page.get_by_label("Spoken language").select_option(language)
    w.page.get_by_role("button", name="Generate captions").click()


def wait_editor(w: Watched):
    expect(w.page.locator("[data-testid=cue-list], .empty")).to_be_visible(timeout=PROCESS_TIMEOUT)


def job_id_of(w: Watched) -> str:
    return w.page.url.split("job=")[1].split("&")[0]


def download(w: Watched, label: str, tmp_path: Path) -> Path:
    with w.page.expect_download() as dl:
        w.page.get_by_role("button", name=label).click()
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    return path


def no_stuck_jobs(stack: Stack):
    deadline = time.time() + 30
    while time.time() < deadline:
        statuses = stack.job_statuses()
        if not any(s in ("pending", "processing") for s in statuses.values()):
            return statuses
        time.sleep(0.5)
    raise AssertionError(f"jobs stuck: {stack.job_statuses()}")


# ── A / B: successful audio and video ────────────────────────────────────────

def test_A_audio_upload_to_srt(browser, main_stack, clips, tmp_path):
    w = new_page(browser, main_stack)
    submit(w, clips["speech_mp3"])
    expect(w.page.get_by_test_id("stage")).to_be_visible()
    wait_editor(w)
    expect(w.page.get_by_label("Caption 1 text")).to_have_value("Welcome to Caption Forge.")
    assert w.page.locator("audio").count() == 1 and w.page.locator("video").count() == 0
    path = download(w, "Download .srt", tmp_path)
    assert path.name == "speech.srt"
    cues = validate_srt(path.read_text(encoding="utf-8"))
    assert len(cues) == w.page.locator("[data-testid=cue-list] li").count() >= 3
    # network: the browser really talked to the backend
    assert [r[2] for r in w.calls("POST", "/api/transcribe")] == [202]
    assert len(w.calls("GET", "/api/status/")) >= 1
    assert all(r[2] == 200 for r in w.calls("GET", "/api/status/"))
    assert [r[2] for r in w.calls("GET", "/api/result/")] == [200]
    assert [r[2] for r in w.calls("GET", "/api/download/")] == [200]
    w.assert_clean_console()
    shot(w.page, "A-audio")


def test_B_video_upload_to_vtt_parsed_by_chrome(browser, main_stack, clips, tmp_path):
    w = new_page(browser, main_stack)
    submit(w, clips["video_webm"])
    wait_editor(w)
    assert w.page.locator("video").count() == 1
    path = download(w, "Download .vtt", tmp_path)
    content = path.read_text(encoding="utf-8")
    cues = validate_vtt(content)
    # Independent check #2: Chrome's own WebVTT parser.
    chrome_cues = w.page.evaluate("""async (vtt) => {
        const v = document.querySelector('video');
        const t = document.createElement('track');
        t.kind = 'subtitles'; t.src = URL.createObjectURL(new Blob([vtt], {type: 'text/vtt'}));
        v.appendChild(t); t.track.mode = 'hidden';
        await new Promise((res, rej) => { t.onload = res; t.onerror = () => rej('track error'); });
        return Array.from(t.track.cues).map(c => [c.startTime, c.endTime, c.text]);
    }""", content)
    assert len(chrome_cues) == len(cues)
    for (s, e, txt), (cs, ce, ct) in zip(cues, chrome_cues):
        assert abs(s - cs) < 1e-3 and abs(e - ce) < 1e-3 and txt == ct
    assert "lazy dog" in " ".join(c[2] for c in chrome_cues)
    w.assert_clean_console()


# ── C / K: editing, persistence, reload, other tab ───────────────────────────

def test_C_K_edit_save_reload_and_reopen(browser, main_stack, clips, tmp_path):
    w = new_page(browser, main_stack)
    submit(w, clips["speech_wav"])
    wait_editor(w)
    p = w.page
    p.get_by_label("Caption 1 text").fill("Bienvenue à CaptionForge — 日本語 ✓ <b>&")
    p.get_by_label("Caption 2 start").fill("00:00:02.300")
    expect(p.get_by_test_id("save-state")).to_have_text("Unsaved changes")
    # invalid edit: overlap with caption 1 → blocked client-side
    p.get_by_label("Caption 2 start").fill("00:00:00.500")
    expect(p.locator("ul.error")).to_contain_text("starts before caption 1 ends")
    expect(p.get_by_role("button", name="Save changes")).to_be_disabled()
    expect(p.get_by_role("button", name="Download .srt")).to_be_disabled()
    p.get_by_label("Caption 2 start").fill("00:00:02.300")
    # insert + delete
    p.get_by_title("Insert caption after").nth(3).click()
    p.get_by_label("Caption 5 text").fill("Added manually.")
    p.get_by_role("button", name="Save changes").click()
    expect(p.get_by_test_id("save-state")).to_contain_text("Saved at")
    assert [r[2] for r in w.calls("PUT", "/api/result/")] == [200]

    job = job_id_of(w)
    p.reload()
    expect(p.get_by_label("Caption 1 text")).to_have_value("Bienvenue à CaptionForge — 日本語 ✓ <b>&",
                                                            timeout=30_000)
    expect(p.get_by_label("Caption 2 start")).to_have_value("00:00:02.300")
    expect(p.get_by_label("Caption 5 text")).to_have_value("Added manually.")

    # K: a completely separate browser context (no shared storage) sees the same job
    other = browser.new_context(accept_downloads=True)
    op = other.new_page()
    op.goto(f"{main_stack.fe_url}/?job={job}")
    expect(op.get_by_label("Caption 1 text")).to_have_value("Bienvenue à CaptionForge — 日本語 ✓ <b>&",
                                                             timeout=30_000)
    expect(op.get_by_test_id("result-meta")).to_contain_text("edited")
    other.close()

    # unsaved change, then download → auto-saved first
    p.get_by_label("Caption 5 text").fill("Changed right before download.")
    path = download(w, "Download .vtt", tmp_path)
    vtt = validate_vtt(path.read_text(encoding="utf-8"))
    assert vtt[0][2] == "Bienvenue à CaptionForge — 日本語 ✓ <b>&"
    assert vtt[-1][2] == "Changed right before download."
    assert vtt[1][0] == pytest.approx(2.3)
    expect(p.get_by_test_id("save-state")).to_contain_text("Saved at")
    srt = validate_srt(main_stack.api().get(f"/api/download/{job}").text)
    assert srt[-1][2] == "Changed right before download."
    w.assert_clean_console()


# ── D: invalid files ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("clip,where,message", [
    ("notes_txt", "client", "not supported"),
    ("empty", "client", "This file is empty"),
    ("corrupt", "server", "could not be read as audio or video"),
    ("text_as_wav", "server", "could not be read as audio or video"),
    ("bad_codec", "server", "codec that cannot be decoded"),
])
def test_D_invalid_files(browser, main_stack, clips, clip, where, message):
    w = new_page(browser, main_stack)
    w.page.get_by_test_id("file-input").set_input_files(str(clips[clip]))
    if where == "client":
        expect(w.page.locator("p.error")).to_contain_text(message)
        expect(w.page.get_by_role("button", name="Generate captions")).to_be_disabled()
        assert w.calls("POST", "/api/transcribe") == []
    else:
        w.page.get_by_role("button", name="Generate captions").click()
        expect(w.page.get_by_test_id("error-message")).to_contain_text(message, timeout=30_000)
        assert [r[2] for r in w.calls("POST", "/api/transcribe")] == [422]
        text = w.page.locator("main").inner_text()
        assert "C:\\" not in text and "Traceback" not in text and "ffmpeg version" not in text
        w.page.get_by_role("button", name="Try another file").click()
        expect(w.page.get_by_text("Drop an audio or video file")).to_be_visible()
        w.assert_clean_console(allowed=[HTTP_ERROR_LOG])
    no_stuck_jobs(main_stack)
    assert list(main_stack.upload_dir.iterdir()) == []


# ── E: silent media ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("clip", ["silence", "silent_video"])
def test_E_silent_media(browser, main_stack, clips, clip, tmp_path):
    w = new_page(browser, main_stack)
    submit(w, clips[clip])
    expect(w.page.get_by_text("No speech was detected")).to_be_visible(timeout=PROCESS_TIMEOUT)
    expect(w.page.get_by_test_id("result-meta")).to_contain_text("0 captions")
    assert validate_srt(download(w, "Download .srt", tmp_path).read_text(encoding="utf-8")) == []
    w.page.get_by_role("button", name="Add caption").click()
    w.page.get_by_label("Caption 1 text").fill("[silence]")
    w.page.get_by_role("button", name="Save changes").click()
    expect(w.page.get_by_test_id("save-state")).to_contain_text("Saved at")
    cues = validate_srt(download(w, "Download .srt", tmp_path).read_text(encoding="utf-8"))
    assert [c[2] for c in cues] == ["[silence]"]
    w.assert_clean_console()


# ── G: cancellation and deletion ─────────────────────────────────────────────

def test_G_cancel_active_job(browser, main_stack, clips):
    w = new_page(browser, main_stack)
    submit(w, clips["long"])
    expect(w.page.get_by_test_id("stage")).to_contain_text("Recognising speech", timeout=60_000)
    job = job_id_of(w)
    w.page.get_by_role("button", name="Cancel job").click()
    expect(w.page.get_by_text("Drop an audio or video file")).to_be_visible()
    assert "job=" not in w.page.url
    assert [r[2] for r in w.calls("DELETE", f"/api/jobs/{job}")] == [204]
    assert main_stack.api().get(f"/api/status/{job}").status_code == 404
    deadline = time.time() + 60
    while (main_stack.upload_dir / job).exists():
        assert time.time() < deadline, "upload not removed after cancel"
        time.sleep(0.3)
    assert not (main_stack.output_dir / job).exists()
    w.assert_clean_console()


def test_G_delete_completed_job(browser, main_stack, clips):
    w = new_page(browser, main_stack)
    submit(w, clips["first_half"])
    wait_editor(w)
    job = job_id_of(w)
    w.page.once("dialog", lambda d: d.accept())
    w.page.get_by_role("button", name="Delete from server").click()
    expect(w.page.get_by_text("Drop an audio or video file")).to_be_visible()
    assert main_stack.api().get(f"/api/status/{job}").status_code == 404
    assert not (main_stack.output_dir / job).exists()
    # reopening the deleted job explains what happened
    w.page.goto(f"{main_stack.fe_url}/?job={job}")
    expect(w.page.get_by_test_id("error-message")).to_contain_text("no longer exists", timeout=30_000)
    w.assert_clean_console(allowed=[HTTP_ERROR_LOG])


# ── Network resilience (regression for frontend defects) ─────────────────────

def test_status_poll_blip_recovers_and_clears_message(browser, main_stack, clips):
    """Regression D9: the 'Retrying…' message stayed on screen after the connection recovered."""
    w = new_page(browser, main_stack)
    submit(w, clips["long"])
    expect(w.page.get_by_test_id("stage")).to_contain_text("Recognising speech", timeout=60_000)
    w.page.route("**/api/status/**", lambda route: route.abort())
    expect(w.page.get_by_text("Retrying")).to_be_visible(timeout=10_000)
    w.page.unroute("**/api/status/**")
    expect(w.page.get_by_text("Retrying")).to_be_hidden(timeout=10_000)
    expect(w.page.get_by_test_id("stage")).to_be_visible()      # still processing normally
    w.page.get_by_role("button", name="Cancel job").click()
    expect(w.page.get_by_text("Drop an audio or video file")).to_be_visible()


def test_download_failure_is_shown_not_navigated(browser, main_stack, clips):
    """Regression D10: a failed download navigated the tab to a raw JSON error page."""
    w = new_page(browser, main_stack)
    submit(w, clips["second_half"])
    wait_editor(w)
    url = w.page.url
    w.page.route("**/api/download/**", lambda route: route.fulfill(
        status=404, content_type="application/json",
        body=json.dumps({"detail": "Subtitle file is missing on the server.", "code": "output_missing"})))
    w.page.get_by_role("button", name="Download .srt").click()
    expect(w.page.get_by_role("alert").filter(has_text="Subtitle file is missing")).to_be_visible()
    assert w.page.url == url
    expect(w.page.get_by_test_id("cue-list")).to_be_visible()


# ── H: restart and recovery ──────────────────────────────────────────────────

def test_H_restart_recovery(browser, main_stack, clips):
    w = new_page(browser, main_stack)
    submit(w, clips["second_half"])
    wait_editor(w)
    done_job = job_id_of(w)

    w2 = new_page(browser, main_stack)
    submit(w2, clips["long"])
    expect(w2.page.get_by_test_id("stage")).to_contain_text("Recognising speech", timeout=60_000)
    running_job = job_id_of(w2)

    main_stack.kill_backend()                       # crash while processing
    expect(w2.page.get_by_text("Retrying")).to_be_visible(timeout=15_000)
    main_stack.backend.start()

    expect(w2.page.get_by_test_id("error-message")).to_contain_text("interrupted", timeout=30_000)
    w.page.reload()
    expect(w.page.get_by_test_id("cue-list")).to_be_visible(timeout=30_000)
    with main_stack.api() as api:
        assert api.get(f"/api/status/{done_job}").json()["status"] == "done"
        st = api.get(f"/api/status/{running_job}").json()
        assert st["status"] == "error" and st["error"]["code"] == "interrupted"
    assert list(main_stack.upload_dir.iterdir()) == []
    # the restarted server processes new work
    w3 = new_page(browser, main_stack)
    submit(w3, clips["first_half"])
    wait_editor(w3)


# ── I: concurrent uploads from three tabs ────────────────────────────────────

def test_I_concurrent_uploads(browser, main_stack, clips):
    files = [clips["speech_wav"], clips["first_half"], clips["second_half"]]
    tabs = [new_page(browser, main_stack) for _ in files]
    for w, f in zip(tabs, files):
        w.page.get_by_test_id("file-input").set_input_files(str(f))
    for w in tabs:
        w.page.get_by_role("button", name="Generate captions").click()
    expect(tabs[2].page.get_by_test_id("stage")).to_contain_text("position", timeout=30_000)
    for w in tabs:
        wait_editor(w)
    ids = [job_id_of(w) for w in tabs]
    assert len(set(ids)) == 3
    texts = [" ".join(w.page.locator("[data-testid=cue-list] textarea").evaluate_all(
        "els => els.map(e => e.value)")) for w in tabs]
    assert "Welcome" in texts[0] and "lazy dog" in texts[0]
    assert "Welcome" in texts[1] and "lazy dog" not in texts[1]
    assert "lazy dog" in texts[2] and "Welcome" not in texts[2]
    with main_stack.api() as api:
        started = [api.get(f"/api/result/{i}").json()["created_at"] for i in ids]
    assert started == sorted(started)


# ── J: repeated processing, including after a failed request ─────────────────

def test_J_repeated_processing(browser, main_stack, clips):
    w = new_page(browser, main_stack)
    results, ids = [], []
    for attempt in range(3):
        if attempt == 1:                                  # a failed request in between
            submit(w, clips["corrupt"])
            expect(w.page.get_by_test_id("error-message")).to_be_visible(timeout=30_000)
            w.page.get_by_role("button", name="Try another file").click()
        submit(w, clips["first_half"])
        wait_editor(w)
        ids.append(job_id_of(w))
        results.append(w.page.locator("[data-testid=cue-list] textarea").evaluate_all(
            "els => els.map(e => e.value)"))
        w.page.get_by_role("button", name="Caption another file").click()
    assert len(set(ids)) == 3
    assert results[0] == results[1] == results[2]          # deterministic, no stale state
    assert list(main_stack.upload_dir.iterdir()) == []
    no_stuck_jobs(main_stack)


# ── UX: keyboard, a11y, mobile, long transcript ──────────────────────────────

def test_keyboard_and_accessibility(browser, main_stack, clips):
    w = new_page(browser, main_stack)
    p = w.page
    p.keyboard.press("Tab")
    assert p.evaluate("document.activeElement.getAttribute('role')") == "button"
    with p.expect_file_chooser() as fc:
        p.keyboard.press("Enter")
    fc.value.set_files(str(clips["first_half"]))
    p.get_by_role("button", name="Generate captions").focus()
    p.keyboard.press("Enter")
    expect(p.get_by_role("progressbar")).to_be_visible(timeout=30_000)
    wait_editor(w)
    unlabeled = p.evaluate("""() => Array.from(document.querySelectorAll(
        'input:not([type=hidden]):not([hidden]), textarea, select, button')).filter(el => {
          const name = el.getAttribute('aria-label') || el.getAttribute('title') ||
                       (el.labels && el.labels.length && el.labels[0].innerText) || el.innerText;
          return !name || !name.trim();
        }).map(el => el.outerHTML.slice(0, 80))""")
    assert unlabeled == []


def test_mobile_layouts(browser, main_stack, clips):
    w = new_page(browser, main_stack, viewport={"width": 360, "height": 740}, is_mobile=True)
    assert w.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    submit(w, clips["speech_wav"])
    wait_editor(w)
    assert w.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(w.page, "mobile-editor")


def test_long_transcript_renders(browser, main_stack, clips, tmp_path):
    w = new_page(browser, main_stack)
    submit(w, clips["long"])
    wait_editor(w)
    n = w.page.locator("[data-testid=cue-list] li").count()
    assert n >= 30
    cues = validate_srt(download(w, "Download .srt", tmp_path).read_text(encoding="utf-8"))
    assert len(cues) == n
    w.assert_clean_console()


# ── Human speech (optional data) ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def human(tmp_path_factory):
    src = os.environ.get("LIBRISPEECH_DIR")
    if not src or not list(Path(src).glob("*.flac")):
        pytest.skip("BLOCKED: LIBRISPEECH_DIR not set (human speech data is not committed)")
    files = sorted(Path(src).glob("*.flac"))[:12]
    d = tmp_path_factory.mktemp("human")
    lst = d / "list.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files))
    audio = d / "librispeech sample.flac"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "flac", str(audio))
    video = d / "librispeech sample.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-i", str(audio), "-shortest",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    manifest = json.loads((Path(src) / "manifest.json").read_text())
    ref = {m["file"]: m["text"] for m in manifest}
    return {"audio": audio, "video": video, "reference": " ".join(ref[f.name] for f in files)}


@pytest.mark.parametrize("kind,fmt", [("audio", "srt"), ("video", "vtt")])
def test_A2_B2_human_speech_workflows(browser, main_stack, human, kind, fmt, tmp_path):
    from tests.test_model_pipeline import wer
    w = new_page(browser, main_stack)
    submit(w, human[kind])
    wait_editor(w)
    path = download(w, f"Download .{fmt}", tmp_path)
    content = path.read_text(encoding="utf-8")
    cues = validate_srt(content) if fmt == "srt" else validate_vtt(content)
    hyp = " ".join(c[2].replace("\n", " ") for c in cues)
    error = wer(human["reference"], hyp)
    print(f"\n[human-e2e] {kind}: {len(cues)} cues, WER {error:.1%}")
    assert error <= 0.15
    w.assert_clean_console()


# ── F / L: constrained stack ─────────────────────────────────────────────────

def test_F_processing_failure_then_recovery(browser, constrained_stack, clips):
    """Real model-load failure (model dir missing) → job error in UI → model
    installed → the same server process completes the next job."""
    w = new_page(browser, constrained_stack)
    submit(w, clips["first_half"])
    expect(w.page.get_by_test_id("error-message")).to_contain_text("Could not load Whisper model",
                                                                    timeout=60_000)
    expect(w.page.get_by_text("Error code: model_unavailable")).to_be_visible()
    no_stuck_jobs(constrained_stack)

    from huggingface_hub import snapshot_download
    # Only the files faster-whisper itself downloads (README etc. are never cached).
    cached = snapshot_download("Systran/faster-whisper-base", local_files_only=True,
                               allow_patterns=["config.json", "preprocessor_config.json",
                                               "model.bin", "tokenizer.json", "vocabulary.*"])
    shutil.copytree(cached, constrained_stack.model_dir)

    w.page.get_by_role("button", name="Try another file").click()
    submit(w, clips["first_half"])
    wait_editor(w)
    expect(w.page.get_by_label("Caption 1 text")).to_have_value("Welcome to Caption Forge.")
    w.assert_clean_console(allowed=[HTTP_ERROR_LOG])


@pytest.mark.parametrize("clip,outcome", [
    ("size_below", "accepted"), ("size_at", "accepted"), ("size_above", "client:maximum is 1 MB"),
    ("dur_9.5", "accepted"), ("dur_10.0", "accepted"), ("dur_10.5", "server:maximum is 10 seconds"),
])
def test_L_resource_limits(browser, constrained_stack, clips, clip, outcome):
    if not constrained_stack.model_dir.exists():
        pytest.skip("depends on test_F installing the model")
    w = new_page(browser, constrained_stack)
    w.page.get_by_test_id("file-input").set_input_files(str(clips[clip]))
    if outcome.startswith("client:"):
        expect(w.page.locator("p.error")).to_contain_text(outcome.split(":", 1)[1])
        with constrained_stack.api() as api, open(clips[clip], "rb") as fh:
            r = api.post("/api/transcribe", files={"file": (clips[clip].name, fh)})
        assert r.status_code == 413 and r.json()["code"] == "file_too_large"
        return
    w.page.get_by_role("button", name="Generate captions").click()
    if outcome == "accepted":
        wait_editor(w)
    else:
        expect(w.page.get_by_test_id("error-message")).to_contain_text(outcome.split(":", 1)[1],
                                                                        timeout=30_000)
        assert [r[2] for r in w.calls("POST", "/api/transcribe")] == [422]
    no_stuck_jobs(constrained_stack)

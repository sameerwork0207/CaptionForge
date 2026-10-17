"""
Helpers for full-stack E2E tests: a real uvicorn backend (real model) plus a
production build of the Next.js frontend wired to it, and a Playwright page
wrapper that records console errors and network traffic.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import pytest

from tests.test_e2e_server import Server

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
NPX = shutil.which("npx") or shutil.which("npx.cmd")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """Backend + production frontend. ``backend.env`` can be tweaked before start."""

    def __init__(self, tmp: Path, dist_dir: str, backend_env: Optional[Dict[str, str]] = None):
        if not NPX or not (FRONTEND / "node_modules").exists():
            pytest.skip("BLOCKED: frontend dependencies not installed (npm install)")
        self.tmp = tmp
        self.backend = Server(tmp)
        self.backend.env.update(backend_env or {})
        self.fe_port = free_port()
        self.fe_url = f"http://127.0.0.1:{self.fe_port}"
        self.backend.env["CORS_ORIGINS"] = self.fe_url
        self.dist_dir = dist_dir
        self.fe = None

    @property
    def output_dir(self) -> Path:
        return Path(self.backend.env["OUTPUT_DIR"])

    @property
    def upload_dir(self) -> Path:
        return Path(self.backend.env["UPLOAD_DIR"])

    def start(self) -> "Stack":
        self.backend.start()
        env = {**os.environ, "NEXT_PUBLIC_API_BASE_URL": self.backend.url,
               "NEXT_DIST_DIR": self.dist_dir, "NEXT_TELEMETRY_DISABLED": "1"}
        # `next build` with a custom distDir adds it to tsconfig.json; keep the tracked file clean.
        tsconfig = FRONTEND / "tsconfig.json"
        original = tsconfig.read_bytes()
        try:
            build = subprocess.run([NPX, "next", "build"], cwd=FRONTEND, env=env, capture_output=True,
                                   text=True, timeout=600, shell=os.name == "nt")
        finally:
            tsconfig.write_bytes(original)
        assert build.returncode == 0, build.stdout[-3000:] + build.stderr[-3000:]
        log = open(self.tmp / "frontend.log", "w")
        self.fe = subprocess.Popen([NPX, "next", "start", "-p", str(self.fe_port), "-H", "127.0.0.1"],
                                   cwd=FRONTEND, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   shell=os.name == "nt")
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(self.fe_url, timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.5)
        raise AssertionError("frontend did not start")

    def stop(self) -> None:
        if self.fe and self.fe.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.fe.pid), "/T", "/F"], capture_output=True)
            else:
                self.fe.terminate()
        self.backend.stop()
        shutil.rmtree(FRONTEND / self.dist_dir, ignore_errors=True)

    def kill_backend(self) -> None:
        """Hard kill (no graceful shutdown), like a crash or power loss."""
        p = self.backend.proc
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.kill()
        p.wait(20)

    def job_statuses(self) -> Dict[str, str]:
        import json
        out = {}
        for f in self.output_dir.glob("*/job.json"):
            out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))["status"]
        return out

    def api(self) -> httpx.Client:
        return httpx.Client(base_url=self.backend.url, timeout=30)


def launch_browser(p):
    for channel in ("chrome", "msedge", None):
        try:
            return p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        except Exception:  # noqa: BLE001 - try the next available browser
            continue
    pytest.skip("BLOCKED: no Chromium-based browser available for Playwright")


class Watched:
    """A Playwright page that records console errors, page errors and API traffic."""

    def __init__(self, page, api_base: str):
        self.page = page
        self.console_errors: List[str] = []
        self.responses: List[tuple] = []
        self.failed_requests: List[str] = []
        page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text))
        page.on("pageerror", lambda e: self.console_errors.append(f"pageerror: {e}"))
        page.on("response", lambda r: r.url.startswith(api_base) and self.responses.append(
            (r.request.method, r.url[len(api_base):], r.status)))
        page.on("requestfailed", lambda r: self.failed_requests.append(r.url))
        self.http_errors: List[tuple] = []        # every >=400 response, any origin
        page.on("response", lambda r: r.status >= 400 and self.http_errors.append((r.status, r.url)))

    def calls(self, method: str, prefix: str):
        return [r for r in self.responses if r[0] == method and r[1].startswith(prefix)]

    def assert_clean_console(self, allowed=()):
        bad = [e for e in self.console_errors if not any(a in e for a in allowed)]
        assert not bad, f"console errors: {bad}; HTTP errors: {self.http_errors}"


def shot(page, name: str):
    d = os.environ.get("E2E_SCREENSHOT_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"), full_page=True)

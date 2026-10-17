"""
Contract test: the TypeScript interfaces in ``frontend/lib/api.ts`` must match
what the backend actually returns (OpenAPI schemas for typed endpoints, live
responses for untyped ones). Catches renamed/removed fields and nullability
drift between the two codebases without running a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import upload, wait_for

API_TS = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "api.ts"

# TS interface → backend schema (OpenAPI component) it mirrors.
TYPED = {"StatusResponse": "JobStatusResponse", "ResultResponse": "TranscriptionResult",
         "CaptionSegment": "CaptionSegment", "JobError": "JobError"}


def parse_interfaces(source: str) -> dict:
    """Very small parser for `export interface X [extends Omit<Y, "a">] { f: T; }`."""
    out = {}
    pattern = re.compile(
        r"export interface (\w+)(?:\s+extends\s+Omit<(\w+),\s*\"(\w+)\">)?\s*\{(.*?)\n\}", re.S)
    for name, base, omitted, body in pattern.findall(source):
        fields = {}
        if base:
            fields.update({k: v for k, v in out[base].items() if k != omitted})
        depth = 0
        for line in body.split("\n"):
            if depth == 0:
                m = re.match(r"\s*(\w+)(\?)?:\s*(.+?);?\s*(//.*)?$", line)
                if m:
                    fields[m.group(1)] = m.group(3).strip().rstrip(";")
            depth += line.count("{") - line.count("}")
        out[name] = fields
    return out


@pytest.fixture(scope="module")
def ts_types():
    if not API_TS.exists():
        pytest.skip("frontend not present")
    return parse_interfaces(API_TS.read_text(encoding="utf-8"))


def _nullable(prop: dict) -> bool:
    return any(alt.get("type") == "null" for alt in prop.get("anyOf", []))


@pytest.mark.parametrize("ts_name,schema_name", TYPED.items())
def test_typed_interfaces_match_openapi(make_client, ts_types, ts_name, schema_name):
    schema = make_client().app.openapi()["components"]["schemas"][schema_name]
    props = schema["properties"]
    ts_fields = ts_types[ts_name]
    assert ts_fields, f"could not parse interface {ts_name}"
    for field, ts_type in ts_fields.items():
        assert field in props, f"{ts_name}.{field} is not returned by the backend"
        assert ("null" in ts_type) == _nullable(props[field]), \
            f"nullability mismatch for {ts_name}.{field}: TS '{ts_type}' vs backend"
    required_by_backend = set(props) - set(ts_fields)
    # Fields the frontend deliberately ignores must be listed here explicitly.
    assert required_by_backend <= {"srt_content", "finished_at"}, required_by_backend


def test_untyped_endpoints_match_live_responses(make_client, ts_types):
    c = make_client()
    cfg, health = c.get("/api/config").json(), c.get("/api/health").json()
    for field in ts_types["ServerConfig"]:
        assert field in cfg, f"ServerConfig.{field} missing from /api/config"
    for field in ts_types["Health"]:
        assert field in health, f"Health.{field} missing from /api/health"
    assert set(health["model"]) == {"name", "device", "active_device", "loaded"}


def test_error_body_shape_matches_client_parser(make_client, media):
    """lib/api.ts reads `detail` (string) and `code` (string) from every error body."""
    c = make_client()
    responses = [
        c.get("/api/status/00000000-0000-0000-0000-000000000000"),
        c.post("/api/transcribe", files={"file": ("x.txt", b"hi")}),
        c.post("/api/transcribe", data={}),
        c.get("/api/download/nope?format=zip"),
    ]
    jid = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, jid)
    responses.append(c.put(f"/api/result/{jid}/segments", json={"segments": [{"start": 2, "end": 1, "text": "x"}]}))
    for r in responses:
        body = r.json()
        assert r.status_code >= 400
        assert isinstance(body["detail"], str) and isinstance(body["code"], str), body


def test_status_values_match(ts_types):
    src = API_TS.read_text(encoding="utf-8")
    m = re.search(r"export type JobStatus = (.+);", src)
    ts_values = set(re.findall(r'"(\w+)"', m.group(1)))
    from app.models.job import JobStatus
    assert ts_values == {s.value for s in JobStatus}

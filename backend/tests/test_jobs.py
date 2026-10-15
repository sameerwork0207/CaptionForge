"""JobManager lifecycle, persistence, recovery, cleanup — with a stub pipeline (no ML)."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import JobCancelledError, MediaError
from app.jobs import JobManager, QueueFullError
from app.pipeline import PipelineResult
from app.services.captions import Caption


def ok_pipeline(input_path, work_dir, settings, mm, **kw):
    kw["on_stage"]("transcribing")
    kw["on_progress"](0.5)
    return PipelineResult("en", 0.9, 3.0, "hello world", [Caption(1, 0.0, 1.0, "hello world")])


def wait(jm, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        rec = jm.get(job_id)
        if rec is None or rec["status"] in ("done", "error"):
            return rec
        time.sleep(0.02)
    raise AssertionError("timeout")


def make_input(jm, job_id):
    d = jm.upload_dir(job_id)
    d.mkdir(parents=True)
    p = d / "input.wav"
    p.write_bytes(b"x")
    return str(p)


@pytest.fixture
def jm_factory(settings):
    made = []

    def _make(pipeline=ok_pipeline, s=settings):
        jm = JobManager(s, model_manager=None, pipeline=pipeline)
        jm.start()
        made.append(jm)
        return jm
    yield _make
    for jm in made:
        jm.stop()


def test_success_lifecycle_and_cleanup(jm_factory, settings):
    seen = []

    def pipeline(*a, **kw):
        seen.append(jm.get(job_id)["status"])
        return ok_pipeline(*a, **kw)

    jm = jm_factory(pipeline)
    job_id = jm.new_job_id()
    rec = jm.submit(job_id, make_input(jm, job_id), "a.wav", None, False)
    assert rec["status"] == "pending"
    rec = wait(jm, job_id)
    assert seen == ["processing"]
    assert rec["status"] == "done" and rec["progress"] == 1.0
    assert rec["segments"][0]["text"] == "hello world"
    assert not jm.upload_dir(job_id).exists()                 # upload removed
    assert jm.subtitle_path(job_id, "srt").read_text(encoding="utf-8").startswith("1\n")
    assert jm.subtitle_path(job_id, "vtt").exists()
    on_disk = json.loads((jm.output_dir(job_id) / "job.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "done" and "input_path" not in on_disk


@pytest.mark.parametrize("exc,code,msg", [
    (MediaError("bad audio", code="decode_failed"), "decode_failed", "bad audio"),
    (RuntimeError("boom C:\\secret\\path"), "internal_error", "unexpected error"),
])
def test_failures_are_recorded_and_worker_survives(jm_factory, exc, code, msg):
    calls = {"n": 0}

    def pipeline(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return ok_pipeline(*a, **kw)

    jm = jm_factory(pipeline)
    j1, j2 = jm.new_job_id(), jm.new_job_id()
    jm.submit(j1, make_input(jm, j1), "a.wav", None, False)
    jm.submit(j2, make_input(jm, j2), "b.wav", None, False)
    r1, r2 = wait(jm, j1), wait(jm, j2)
    assert r1["status"] == "error" and r1["error"]["code"] == code
    assert msg in r1["error"]["message"] and "secret" not in r1["error"]["message"]
    assert r2["status"] == "done"
    assert not jm.upload_dir(j1).exists()


def test_jobs_run_serially_in_fifo_order(jm_factory):
    order, active, max_active = [], [0], [0]
    lock = threading.Lock()

    def pipeline(input_path, *a, **kw):
        with lock:
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
        order.append(input_path)
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return ok_pipeline(input_path, *a, **kw)

    jm = jm_factory(pipeline)
    ids = [jm.new_job_id() for _ in range(4)]
    paths = [make_input(jm, j) for j in ids]
    for j, p in zip(ids, paths):
        jm.submit(j, p, "x.wav", None, False)
    assert jm.get(ids[-1])["queue_position"] >= 2
    for j in ids:
        assert wait(jm, j)["status"] == "done"
    assert order == paths and max_active[0] == 1
    assert len({str(jm.output_dir(j)) for j in ids}) == 4      # no shared output files


def test_queue_limit(jm_factory, settings):
    from dataclasses import replace
    gate = threading.Event()

    def pipeline(*a, **kw):
        gate.wait(5)
        return ok_pipeline(*a, **kw)

    jm = jm_factory(pipeline, replace(settings, max_queued_jobs=2))
    for _ in range(2):
        j = jm.new_job_id()
        jm.submit(j, make_input(jm, j), "x.wav", None, False)
    with pytest.raises(QueueFullError):
        jm.ensure_capacity()
    gate.set()


def test_cancel_processing_job(jm_factory):
    started = threading.Event()

    def pipeline(*a, should_cancel, **kw):
        started.set()
        for _ in range(200):
            if should_cancel():
                raise JobCancelledError("cancelled")
            time.sleep(0.01)
        return ok_pipeline(*a, **kw)

    jm = jm_factory(pipeline)
    j = jm.new_job_id()
    jm.submit(j, make_input(jm, j), "x.wav", None, False)
    assert started.wait(5)
    assert jm.delete(j) is True
    assert jm.get(j) is None
    time.sleep(0.3)
    assert not jm.upload_dir(j).exists() and not jm.output_dir(j).exists()


def test_delete_pending_job_is_skipped(jm_factory):
    gate = threading.Event()
    ran = []

    def pipeline(input_path, *a, **kw):
        ran.append(input_path)
        gate.wait(5)
        return ok_pipeline(input_path, *a, **kw)

    jm = jm_factory(pipeline)
    j1, j2 = jm.new_job_id(), jm.new_job_id()
    p1, p2 = make_input(jm, j1), make_input(jm, j2)
    jm.submit(j1, p1, "a.wav", None, False)
    jm.submit(j2, p2, "b.wav", None, False)
    assert jm.delete(j2)
    gate.set()
    wait(jm, j1)
    time.sleep(0.1)
    assert ran == [p1] and not jm.upload_dir(j2).exists()


def test_restart_recovers_done_and_marks_interrupted(jm_factory, settings):
    jm = jm_factory()
    done = jm.new_job_id()
    jm.submit(done, make_input(jm, done), "a.wav", None, False)
    wait(jm, done)
    jm.stop()

    # Simulate a job that was mid-processing when the server died.
    stuck = "11111111-2222-3333-4444-555555555555"
    (settings.output_dir / stuck).mkdir()
    (settings.output_dir / stuck / "job.json").write_text(json.dumps(
        {"job_id": stuck, "status": "processing", "filename": "x.wav"}), encoding="utf-8")
    (settings.upload_dir / stuck).mkdir()
    (settings.upload_dir / ".gitkeep").write_text("")

    jm2 = jm_factory()
    assert jm2.get(done)["status"] == "done"
    rec = jm2.get(stuck)
    assert rec["status"] == "error" and rec["error"]["code"] == "interrupted"
    assert not (settings.upload_dir / stuck).exists()
    assert (settings.upload_dir / ".gitkeep").exists()          # unrelated files untouched


def test_retention_purges_old_results(jm_factory):
    jm = jm_factory()
    j = jm.new_job_id()
    jm.submit(j, make_input(jm, j), "a.wav", None, False)
    wait(jm, j)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    jm._update(j, finished_at=old)
    assert jm.purge_expired() == 1
    assert jm.get(j) is None and not jm.output_dir(j).exists()


def test_update_captions_rewrites_exports(jm_factory):
    jm = jm_factory()
    j = jm.new_job_id()
    jm.submit(j, make_input(jm, j), "a.wav", None, False)
    wait(jm, j)
    jm.update_captions(j, [Caption(1, 0.5, 2.0, "edited text")])
    assert "edited text" in jm.subtitle_path(j, "srt").read_text(encoding="utf-8")
    assert "edited text" in jm.subtitle_path(j, "vtt").read_text(encoding="utf-8")
    assert jm.get(j)["edited"] is True

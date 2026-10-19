"""
Phase 0 safeguards: stable caption ids, job revisions and backward compatibility.

* Records written before ids/revisions existed (``fixtures/legacy_job_v1.json``) still load,
  get deterministic ids, and are not rewritten until the user saves.
* Older clients (no ids, no ``base_revision``) behave exactly as before; their exports are
  byte-identical to the golden files.
* An id is never reused for a different caption within a job.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.jobs import JobManager, RevisionConflictError
from app.pipeline import PipelineResult
from app.services.captions import Caption, CaptionValidationError, normalize_edited_captions
from tests.conftest import upload, wait_for
from tests.test_golden_exports import OLD_CLIENT_EDITS, golden

LEGACY = Path(__file__).parent / "fixtures" / "legacy_job_v1.json"
LEGACY_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def install_legacy(settings, mutate=None) -> Path:
    """Copy the legacy record into the output dir, as a previous version would have left it."""
    out = settings.output_dir / LEGACY_ID
    out.mkdir(parents=True)
    record = json.loads(LEGACY.read_text(encoding="utf-8"))
    record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")  # not expired
    if mutate:
        mutate(record)
    path = out / "job.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def stub_pipeline(input_path, work_dir, settings, mm, **kw):
    # Like the existing job tests: captions come back without ids.
    return PipelineResult("en", 0.9, 9.0, "one two three", [
        Caption(1, 0.0, 1.0, "one"), Caption(2, 1.5, 2.5, "two"), Caption(3, 3.0, 4.0, "three")])


@pytest.fixture
def jm_factory(settings):
    made = []

    def _make(pipeline=stub_pipeline):
        jm = JobManager(settings, model_manager=None, pipeline=pipeline)
        jm.start()
        made.append(jm)
        return jm
    yield _make
    for jm in made:
        jm.stop()


def finish_job(jm) -> str:
    job_id = jm.new_job_id()
    d = jm.upload_dir(job_id)
    d.mkdir(parents=True)
    (d / "input.wav").write_bytes(b"x")
    jm.submit(job_id, str(d / "input.wav"), "a.wav", None, False)
    end = time.time() + 10
    while time.time() < end:
        if jm.get(job_id)["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert jm.get(job_id)["status"] == "done"
    return job_id


def ids(segments):
    return [s["id"] for s in segments]


# ── Legacy records ───────────────────────────────────────────────────────────

def test_legacy_record_gets_deterministic_ids_and_revision(jm_factory, settings):
    install_legacy(settings)
    jm = jm_factory()
    rec = jm.get(LEGACY_ID)
    assert rec["status"] == "done" and rec["edited"] is False
    assert ids(rec["segments"]) == ["c1", "c2", "c3"]
    assert rec["revision"] == 1
    # Everything else about the caption is untouched.
    assert [(s["index"], s["start"], s["end"], s["text"]) for s in rec["segments"]] == [
        (1, 0.0, 2.5, "Welcome to CaptionForge."),
        (2, 2.6, 5.0, "This is a legacy record.\nTwo lines."),
        (3, 5.2, 8.0, "It has no ids.")]


def test_loading_a_legacy_record_does_not_rewrite_it(jm_factory, settings):
    path = install_legacy(settings)
    before = path.read_bytes()
    jm_factory()
    assert path.read_bytes() == before


def test_legacy_ids_are_identical_after_every_restart(jm_factory, settings):
    install_legacy(settings)
    jm = jm_factory()
    first = ids(jm.get(LEGACY_ID)["segments"])
    jm.stop()
    assert ids(jm_factory().get(LEGACY_ID)["segments"]) == first


def test_legacy_record_survives_save_and_reload(jm_factory, settings):
    install_legacy(settings)
    jm = jm_factory()
    segs = jm.get(LEGACY_ID)["segments"]
    jm.update_captions(LEGACY_ID, [Caption(1, s["start"], s["end"], s["text"] + "!", s["id"])
                                   for s in segs])
    jm.stop()
    rec = jm_factory().get(LEGACY_ID)
    assert ids(rec["segments"]) == ["c1", "c2", "c3"] and rec["revision"] == 2
    assert rec["edited"] is True and rec["segments"][0]["text"] == "Welcome to CaptionForge.!"


@pytest.mark.parametrize("bad", ["x", 0, -3, True, 1.5, None, [1]])
def test_malformed_revision_is_reset_to_one(jm_factory, settings, bad):
    install_legacy(settings, lambda r: r.update(revision=bad))
    assert jm_factory().get(LEGACY_ID)["revision"] == 1


def test_valid_revision_is_kept(jm_factory, settings):
    install_legacy(settings, lambda r: r.update(revision=7))
    assert jm_factory().get(LEGACY_ID)["revision"] == 7


def test_duplicate_or_invalid_ids_in_a_record_are_repaired(jm_factory, settings):
    def mutate(r):
        r["segments"][0]["id"] = "keep"
        r["segments"][1]["id"] = "keep"        # duplicate
        r["segments"][2]["id"] = ""            # invalid
    install_legacy(settings, mutate)
    got = ids(jm_factory().get(LEGACY_ID)["segments"])
    assert got[0] == "keep" and len(set(got)) == 3 and "" not in got


def test_legacy_job_with_no_speech_loads(jm_factory, settings):
    install_legacy(settings, lambda r: r.update(segments=[], transcript=""))
    rec = jm_factory().get(LEGACY_ID)
    assert rec["segments"] == [] and rec["revision"] == 1


def test_error_and_unfinished_records_have_no_revision(jm_factory, settings):
    install_legacy(settings, lambda r: r.update(status="error", segments=None,
                                                error={"code": "x", "message": "y"}))
    rec = jm_factory().get(LEGACY_ID)
    assert rec["segments"] is None and rec.get("revision") is None


def test_pipeline_output_gets_ids_and_revision_one(jm_factory):
    jm = jm_factory()
    rec = jm.get(finish_job(jm))
    assert ids(rec["segments"]) == ["c1", "c2", "c3"] and rec["revision"] == 1


# ── Ids across edits ────────────────────────────────────────────────────────

def test_ids_follow_their_caption_through_edit_and_reorder(jm_factory):
    jm = jm_factory()
    j = finish_job(jm)
    # Swap the first two captions' times and edit the third; ids stay with their text.
    edited = normalize_edited_captions([   # as the route does: sorts by start and renumbers
        {"start": 1.5, "end": 2.5, "text": "one", "id": "c1"},
        {"start": 0.0, "end": 1.0, "text": "two", "id": "c2"},
        {"start": 3.0, "end": 4.0, "text": "three, edited", "id": "c3"}])
    jm.update_captions(j, edited)
    segs = jm.get(j)["segments"]
    assert [(s["index"], s["text"], s["id"]) for s in segs] == [
        (1, "two", "c2"), (2, "one", "c1"), (3, "three, edited", "c3")]


def test_deleted_ids_are_never_reused(jm_factory):
    jm = jm_factory()
    j = finish_job(jm)
    jm.update_captions(j, [Caption(0, 0.0, 1.0, "one", "c1"), Caption(0, 3.0, 4.0, "three", "c3")])
    seen = {"c1", "c2", "c3"}
    # A stale client sends the deleted id back, and another new caption has no id.
    jm.update_captions(j, [Caption(0, 0.0, 1.0, "one", "c1"), Caption(0, 1.5, 2.5, "new", "c2"),
                           Caption(0, 3.0, 4.0, "three", "c3"), Caption(0, 5.0, 6.0, "newer")])
    segs = jm.get(j)["segments"]
    by_text = {s["text"]: s["id"] for s in segs}
    assert by_text["one"] == "c1" and by_text["three"] == "c3"
    assert by_text["new"] not in seen and by_text["newer"] not in seen
    assert len(set(ids(segs))) == 4


def test_unknown_client_chosen_id_is_not_accepted(jm_factory):
    jm = jm_factory()
    j = finish_job(jm)
    jm.update_captions(j, [Caption(0, 0.0, 1.0, "one", "c1"), Caption(0, 1.5, 2.5, "x", "chosen")])
    got = ids(jm.get(j)["segments"])
    assert got[0] == "c1" and got[1] != "chosen"


def test_issued_ids_survive_restart(jm_factory):
    jm = jm_factory()
    j = finish_job(jm)
    jm.update_captions(j, [Caption(0, 0.0, 1.0, "one", "c1")])      # c2, c3 deleted
    jm.stop()
    jm2 = jm_factory()
    rec = jm2.get(j)
    assert ids(rec["segments"]) == ["c1"] and rec["revision"] == 2
    assert set(rec["cue_ids_issued"]) >= {"c1", "c2", "c3"}
    jm2.update_captions(j, [Caption(0, 0.0, 1.0, "one", "c1"), Caption(0, 2.0, 3.0, "again", "c2")])
    assert jm2.get(j)["segments"][1]["id"] not in {"c1", "c2", "c3"}


def test_normalize_carries_ids_and_rejects_bad_ones():
    caps = normalize_edited_captions([{"start": 5, "end": 6, "text": "b", "id": "x"},
                                      {"start": 1, "end": 2, "text": "a", "id": "y"},
                                      {"start": 8, "end": 9, "text": "c"}])
    assert [(c.text, c.id) for c in caps] == [("a", "y"), ("b", "x"), ("c", None)]
    with pytest.raises(CaptionValidationError, match="duplicate caption id"):
        normalize_edited_captions([{"start": 1, "end": 2, "text": "a", "id": "x"},
                                   {"start": 3, "end": 4, "text": "b", "id": "x"}])
    with pytest.raises(CaptionValidationError, match="id must be"):
        normalize_edited_captions([{"start": 1, "end": 2, "text": "a", "id": ""}])


# ── Revisions ────────────────────────────────────────────────────────────────

def test_revision_increments_per_save_and_conflicts_only_when_requested(jm_factory):
    jm = jm_factory()
    j = finish_job(jm)

    def cap():
        return [Caption(0, 0.0, 1.0, "one", "c1")]

    assert jm.update_captions(j, cap())["revision"] == 2                     # no base_revision
    assert jm.update_captions(j, cap(), base_revision=2)["revision"] == 3    # matches
    with pytest.raises(RevisionConflictError):
        jm.update_captions(j, [Caption(0, 0.0, 1.0, "stale")], base_revision=2)
    rec = jm.get(j)
    assert rec["revision"] == 3 and rec["segments"][0]["text"] == "one"      # nothing changed
    assert jm.update_captions(j, cap())["revision"] == 4                     # old clients still save


# ── Through the HTTP API ─────────────────────────────────────────────────────

def put(c, job_id, body):
    return c.put(f"/api/result/{job_id}/segments", json=body)


def test_api_legacy_job_old_client_edit_matches_golden(make_client, settings):
    install_legacy(settings)
    c = make_client()
    res = c.get(f"/api/result/{LEGACY_ID}").json()
    assert res["revision"] == 1 and ids(res["segments"]) == ["c1", "c2", "c3"]
    r = put(c, LEGACY_ID, OLD_CLIENT_EDITS)                  # no ids, no base_revision
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["edited"] is True
    for fmt in ("srt", "vtt"):
        assert c.get(f"/api/download/{LEGACY_ID}?format={fmt}").content == golden(f"api_edited.{fmt}")


def test_api_exports_ignore_ids_and_revision(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    res = wait_for(c, job_id)
    assert res["revision"] == 1 and ids(res["segments"]) == ["c1", "c2", "c3", "c4"]
    for fmt in ("srt", "vtt"):                                # ids/revision never leak into exports
        assert c.get(f"/api/download/{job_id}?format={fmt}").content == golden(f"api_default.{fmt}")
    with_ids = {"segments": [dict(OLD_CLIENT_EDITS["segments"][0], id="c2"),
                             dict(OLD_CLIENT_EDITS["segments"][1], id="c1"),
                             dict(OLD_CLIENT_EDITS["segments"][2])], "base_revision": 1}
    r = put(c, job_id, with_ids)
    assert r.status_code == 200, r.text
    assert [(s["text"], s["id"]) for s in r.json()["segments"]][:2] == [
        ("First line\nsecond line", "c1"), ("Zweiter Satz — ünïcode", "c2")]
    for fmt in ("srt", "vtt"):
        assert c.get(f"/api/download/{job_id}?format={fmt}").content == golden(f"api_edited.{fmt}")


def test_api_revision_conflict_and_validation(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    r = put(c, job_id, {**OLD_CLIENT_EDITS, "base_revision": 5})
    assert r.status_code == 409 and r.json()["code"] == "revision_conflict"
    after = c.get(f"/api/result/{job_id}").json()
    assert after["revision"] == 1 and after["edited"] is False               # rejected save changed nothing
    for bad in (0, -1, "x"):
        assert put(c, job_id, {**OLD_CLIENT_EDITS, "base_revision": bad}).status_code == 422
    dup = {"segments": [{"start": 0, "end": 1, "text": "a", "id": "c1"},
                        {"start": 2, "end": 3, "text": "b", "id": "c1"}]}
    r = put(c, job_id, dup)
    assert r.status_code == 422 and r.json()["code"] == "invalid_captions"
    assert put(c, job_id, {"segments": [{"start": 0, "end": 1, "text": "a", "id": ""}]}).status_code == 422
    assert put(c, job_id, OLD_CLIENT_EDITS).status_code == 200               # still usable


def test_api_save_and_reload_keeps_ids_and_revision(make_client, media):
    c = make_client()
    job_id = upload(c, media.speech_wav).json()["job_id"]
    wait_for(c, job_id)
    saved = put(c, job_id, {"segments": [{"start": 0, "end": 1, "text": "kept", "id": "c3"},
                                         {"start": 2, "end": 3, "text": "brand new"}]}).json()
    assert saved["revision"] == 2 and saved["segments"][0]["id"] == "c3"
    new_id = saved["segments"][1]["id"]
    assert new_id not in {"c1", "c2", "c3", "c4"}
    reloaded = make_client()                                  # a second app on the same directories
    again = reloaded.get(f"/api/result/{job_id}").json()
    assert again["revision"] == 2 and [s["id"] for s in again["segments"]] == ["c3", new_id]
    assert again["segments"] == saved["segments"]

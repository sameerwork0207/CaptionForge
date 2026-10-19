"""
Job manager: queueing, a single background worker, persistence and cleanup.

Design (deliberately simple for a single-machine app):

* Jobs are processed **one at a time** by one worker thread. Whisper on CPU
  already uses all cores, so parallel jobs would only slow each other down.
  Extra jobs wait in a FIFO queue (bounded by ``MAX_QUEUED_JOBS``).
* Each job's record is written to ``outputs/<job_id>/job.json`` on every status
  change, next to ``captions.srt`` / ``captions.vtt``. Finished jobs therefore
  survive a server restart. Jobs that were pending/processing when the server
  stopped are marked ``error`` with code ``interrupted`` at the next startup;
  they are **not** resumed (the upload is deleted after processing anyway).
* Uploads live in ``uploads/<job_id>/`` and are deleted as soon as the job
  finishes, fails or is cancelled. Finished jobs (results) are deleted
  ``JOB_RETENTION_HOURS`` after they finish (0 = keep forever).
* Deleting a job removes its files. Deleting a job that is processing also
  cancels it; cancellation takes effect at the next transcription segment.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import Settings
from app.errors import CaptionForgeError, JobCancelledError
from app.models.job import JobStatus
from app.pipeline import run_pipeline
from app.services import subtitles
from app.services.captions import Caption, initial_cue_id, new_cue_id

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ACTIVE = (JobStatus.PENDING.value, JobStatus.PROCESSING.value)


def is_valid_job_id(job_id: str) -> bool:
    return bool(_UUID_RE.match(job_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _caption_dicts(captions: List[Caption]) -> List[dict]:
    return [{"index": c.index, "id": c.id, "start": c.start, "end": c.end, "text": c.text}
            for c in captions]


def _backfill_cue_state(record: Dict[str, Any]) -> None:
    """
    Give a finished job's captions stable ids and a revision, in place.

    Records written before ids existed have neither. They get the deterministic ids
    ``c1..cN`` (by position), so reloading the same file after a restart always yields the
    same ids. ``cue_ids_issued`` remembers every id ever used in the job, so an id of a
    deleted caption is never handed to a different one. Malformed or duplicate ids are
    replaced; valid ids are never changed.
    """
    segments = record.get("segments")
    if segments is None:
        return
    raw_issued = record.get("cue_ids_issued")
    issued = {i for i in raw_issued if isinstance(i, str)} if isinstance(raw_issued, list) else set()
    taken: set = set()
    for seg in segments:
        cid = seg.get("id")
        if isinstance(cid, str) and cid and cid not in taken:
            taken.add(cid)
        else:
            seg["id"] = None
    for pos, seg in enumerate(segments, start=1):
        if seg["id"] is None:
            cid = initial_cue_id(pos)
            while cid in taken or cid in issued:
                cid = new_cue_id()
            seg["id"] = cid
            taken.add(cid)
    record["cue_ids_issued"] = sorted(taken | issued)
    rev = record.get("revision")
    if not (isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1):
        record["revision"] = 1


INTERRUPTED_ERROR = {"code": "interrupted",
                     "message": "Processing was interrupted by a server restart. "
                                "Please upload the file again."}


class JobNotFoundError(CaptionForgeError):
    code = "not_found"
    http_status = 404


class QueueFullError(CaptionForgeError):
    code = "queue_full"
    http_status = 503


class RevisionConflictError(CaptionForgeError):
    code = "revision_conflict"
    http_status = 409


class JobManager:
    def __init__(self, settings: Settings, model_manager, pipeline: Callable = run_pipeline):
        self.settings = settings
        self.model_manager = model_manager
        self.pipeline = pipeline
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []           # pending job ids in FIFO order
        self._cancel: set[str] = set()
        self._lock = threading.RLock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stopping = False
        self._worker: Optional[threading.Thread] = None

    # ── paths ────────────────────────────────────────────────────────────────
    def upload_dir(self, job_id: str) -> Path:
        return self.settings.upload_dir / job_id

    def output_dir(self, job_id: str) -> Path:
        return self.settings.output_dir / job_id

    def subtitle_path(self, job_id: str, fmt: str) -> Path:
        return self.output_dir(job_id) / f"captions.{fmt}"

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        self._recover()
        self.purge_expired()
        self._worker = threading.Thread(target=self._run_worker, name="caption-worker",
                                         daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """
        Graceful shutdown: the in-flight job stops at its next segment and is
        recorded as ``interrupted``; pending jobs are left untouched on disk and
        are marked ``interrupted`` by ``_recover`` at the next start.
        """
        self._stopping = True
        if self._worker and self._worker.is_alive():
            self._queue.put(None)
            self._worker.join(timeout)

    @staticmethod
    def _sanitize_record(record: Any, dir_name: str) -> Optional[Dict[str, Any]]:
        """Return a usable job record, or None if the persisted data is malformed."""
        if not isinstance(record, dict) or record.get("job_id") != dir_name \
                or not is_valid_job_id(dir_name):
            return None
        if record.get("status") not in {s.value for s in JobStatus}:
            return None
        segments = record.get("segments")
        if segments is not None:
            if not isinstance(segments, list):
                return None
            for s in segments:
                if not (isinstance(s, dict) and isinstance(s.get("text"), str)
                        and all(isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)
                                for k in ("index", "start", "end"))):
                    return None
        if record.get("error") is not None and not (
                isinstance(record["error"], dict)
                and isinstance(record["error"].get("code"), str)
                and isinstance(record["error"].get("message"), str)):
            return None
        if _parse_time(record.get("finished_at")) is None and record.get("finished_at") is not None:
            record["finished_at"] = _now()        # unparsable → expire from now
        _backfill_cue_state(record)
        return record

    def _recover(self) -> None:
        """Load persisted jobs; mark interrupted ones; remove orphaned uploads."""
        for meta in self.settings.output_dir.glob("*/job.json"):
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable job record %s: %s", meta, exc)
                continue
            record = self._sanitize_record(raw, meta.parent.name)
            if record is None:
                logger.warning("Skipping malformed job record %s", meta)
                continue
            job_id = record["job_id"]
            if record["status"] in _ACTIVE:
                record.update(status=JobStatus.ERROR.value, stage=None, finished_at=_now(),
                              error=dict(INTERRUPTED_ERROR))
                self._jobs[job_id] = record
                self._persist(job_id)
            else:
                self._jobs[job_id] = record
        for entry in self.settings.upload_dir.iterdir():
            if not is_valid_job_id(entry.name.split(".")[0]):
                continue      # only touch files this app created (named by job id)
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        if self._jobs:
            logger.info("Recovered %d job record(s) from disk.", len(self._jobs))

    def purge_expired(self) -> int:
        hours = self.settings.job_retention_hours
        if hours <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        expired = []
        with self._lock:
            for job_id, rec in self._jobs.items():
                fin = _parse_time(rec.get("finished_at"))
                if rec["status"] not in _ACTIVE and fin and fin < cutoff:
                    expired.append(job_id)
        for job_id in expired:
            self.delete(job_id)
        if expired:
            logger.info("Purged %d expired job(s).", len(expired))
        return len(expired)

    # ── persistence ──────────────────────────────────────────────────────────
    def _persist(self, job_id: str) -> None:
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        out = self.output_dir(job_id)
        out.mkdir(parents=True, exist_ok=True)
        tmp = out / "job.json.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out / "job.json")

    def _update(self, job_id: str, persist: bool = True, **fields) -> bool:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return False
            rec.update(fields)
            rec["updated_at"] = _now()
            if persist:
                self._persist(job_id)
            return True

    # ── public API ───────────────────────────────────────────────────────────
    def new_job_id(self) -> str:
        return str(uuid.uuid4())

    def ensure_capacity(self) -> None:
        with self._lock:
            active = sum(1 for r in self._jobs.values() if r["status"] in _ACTIVE)
        if active >= self.settings.max_queued_jobs:
            raise QueueFullError("The server is busy with other files. Please try again later.")

    def submit(self, job_id: str, input_path: str, filename: str,
               language: Optional[str], has_video: bool) -> Dict[str, Any]:
        self.ensure_capacity()
        record = {
            "job_id": job_id, "status": JobStatus.PENDING.value, "stage": "queued",
            "progress": 0.0, "filename": filename, "input_path": input_path,
            "language_requested": language, "has_video": has_video,
            "created_at": _now(), "updated_at": _now(), "started_at": None,
            "finished_at": None, "language": None, "language_probability": None,
            "duration": None, "transcript": None, "segments": None, "edited": False,
            "revision": None, "cue_ids_issued": None, "model": self.settings.whisper_model, "error": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._order.append(job_id)
            self._persist(job_id)
        self._queue.put(job_id)
        logger.info("Job %s queued (%s)", job_id, filename)
        self.purge_expired()
        return copy.deepcopy(record)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            rec = copy.deepcopy(rec)
            rec["queue_position"] = (self._order.index(job_id) + 1
                                     if job_id in self._order else None)
            return rec

    def delete(self, job_id: str) -> bool:
        with self._lock:
            rec = self._jobs.pop(job_id, None)
            if rec is None:
                return False
            if job_id in self._order:
                self._order.remove(job_id)
            if rec["status"] == JobStatus.PROCESSING.value:
                self._cancel.add(job_id)
        shutil.rmtree(self.output_dir(job_id), ignore_errors=True)
        if rec["status"] != JobStatus.PROCESSING.value:
            # The worker cleans up the upload of a processing job itself.
            shutil.rmtree(self.upload_dir(job_id), ignore_errors=True)
        logger.info("Job %s deleted.", job_id)
        return True

    def update_captions(self, job_id: str, captions: List[Caption],
                        base_revision: Optional[int] = None) -> Dict[str, Any]:
        """
        Replace the captions with an edited version and bump the revision.

        Captions that carry the id of an existing caption keep it. Captions without an id,
        or with an id that is not currently in the job (new, or deleted earlier), get a
        fresh id; an id that was ever issued is never reused. ``base_revision`` is only
        checked when given, so clients that do not send it are never rejected.
        """
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:      # deleted between the route's lookup and now
                raise JobNotFoundError(f"Job '{job_id}' not found.")
            revision = rec.get("revision") or 1
            if base_revision is not None and base_revision != revision:
                raise RevisionConflictError(
                    "The captions were changed since you loaded them (revision "
                    f"{revision}, yours was {base_revision}). Reload before saving.")
            current = {s.get("id") for s in rec.get("segments") or []} - {None}
            issued = set(rec.get("cue_ids_issued") or []) | current
            for cap in captions:
                if cap.id is None or cap.id not in current:
                    cap.id = new_cue_id()
                    while cap.id in issued:
                        cap.id = new_cue_id()
                issued.add(cap.id)
            self._write_exports(job_id, captions)
            self._update(job_id, segments=_caption_dicts(captions), edited=True,
                         revision=revision + 1, cue_ids_issued=sorted(issued))
            return copy.deepcopy(self._jobs[job_id])

    def _write_exports(self, job_id: str, captions: List[Caption]) -> None:
        for fmt in subtitles.FORMATS:
            subtitles.write_subtitle(captions, fmt, str(self.subtitle_path(job_id, fmt)))

    # ── worker ───────────────────────────────────────────────────────────────
    def _run_worker(self) -> None:
        while True:
            try:
                job_id = self._queue.get(timeout=600)
            except queue.Empty:
                self.purge_expired()
                continue
            if job_id is None or self._stopping:
                return            # pending jobs stay on disk; recovered as interrupted
            with self._lock:
                if job_id not in self._jobs or job_id not in self._order:
                    continue          # deleted while queued
                self._order.remove(job_id)
                input_path = self._jobs[job_id]["input_path"]
                language = self._jobs[job_id]["language_requested"]
            self._process(job_id, input_path, language)

    def _process(self, job_id: str, input_path: str, language: Optional[str]) -> None:
        self._update(job_id, status=JobStatus.PROCESSING.value, started_at=_now())
        work_dir = str(self.upload_dir(job_id))
        try:
            result = self.pipeline(
                input_path, work_dir, self.settings, self.model_manager,
                language=language,
                on_stage=lambda s: self._update(job_id, stage=s),
                on_progress=lambda p: self._update(job_id, persist=False,
                                                   progress=round(p, 3)),
                should_cancel=lambda: self._stopping or job_id in self._cancel,
            )
            with self._lock:
                if job_id not in self._jobs:
                    raise JobCancelledError("deleted")
                self._update(job_id, stage="exporting")
                self._write_exports(job_id, result.captions)
                cue_state = {"segments": _caption_dicts(result.captions)}
                _backfill_cue_state(cue_state)          # ids c1..cN, revision 1
                self._update(job_id, status=JobStatus.DONE.value, stage=None, progress=1.0,
                             finished_at=_now(), language=result.language,
                             language_probability=round(result.language_probability, 3),
                             duration=result.duration, transcript=result.transcript,
                             segments=cue_state["segments"], revision=cue_state["revision"],
                             cue_ids_issued=cue_state["cue_ids_issued"])
            logger.info("Job %s completed: %d captions", job_id, len(result.captions))
        except JobCancelledError:
            with self._lock:
                user_cancelled = job_id in self._cancel or job_id not in self._jobs
            if user_cancelled:
                logger.info("Job %s cancelled.", job_id)
                with self._lock:
                    self._jobs.pop(job_id, None)
                shutil.rmtree(self.output_dir(job_id), ignore_errors=True)
            else:   # server shutdown: keep the record so clients see why it stopped
                logger.info("Job %s interrupted by shutdown.", job_id)
                self._update(job_id, status=JobStatus.ERROR.value, stage=None,
                             finished_at=_now(), error=dict(INTERRUPTED_ERROR))
        except CaptionForgeError as exc:
            logger.warning("Job %s failed [%s]: %s", job_id, exc.code, exc.message)
            self._update(job_id, status=JobStatus.ERROR.value, stage=None,
                         finished_at=_now(), error={"code": exc.code, "message": exc.message})
        except Exception:  # noqa: BLE001 - must never kill the worker thread
            logger.exception("Job %s failed with an unexpected error", job_id)
            self._update(job_id, status=JobStatus.ERROR.value, stage=None, finished_at=_now(),
                         error={"code": "internal_error",
                                "message": "An unexpected error occurred while processing "
                                           "this file. See the server log for details."})
        finally:
            with self._lock:
                self._cancel.discard(job_id)
            shutil.rmtree(work_dir, ignore_errors=True)
            with self._lock:
                rec = self._jobs.get(job_id)
                if rec is not None:
                    rec.pop("input_path", None)
                    self._persist(job_id)

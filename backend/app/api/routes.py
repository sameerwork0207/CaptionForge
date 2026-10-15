"""
CaptionForge API routes. Handlers only translate HTTP <-> services; all
processing lives in ``app.pipeline`` / ``app.jobs`` / ``app.services``.

  GET    /api/health                       liveness + dependency status
  GET    /api/config                       limits, formats, languages (for the UI)
  POST   /api/transcribe                   upload media, start a job (202)
  GET    /api/status/{job_id}              lightweight polling
  GET    /api/result/{job_id}              full result (captions once done)
  PUT    /api/result/{job_id}/segments     save edited captions
  GET    /api/download/{job_id}?format=    download .srt (default) or .vtt
  DELETE /api/jobs/{job_id}                cancel and/or delete a job
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.errors import CaptionForgeError, MediaError
from app.jobs import JobManager, is_valid_job_id
from app.models.job import (CaptionSegment, CreateJobResponse, ErrorResponse, JobError,
                            JobStatus, JobStatusResponse, TranscriptionResult,
                            UpdateCaptionsRequest)
from app.services import audio as audio_svc
from app.services import subtitles
from app.services import transcription as transcription_svc
from app.services.captions import CaptionValidationError, normalize_edited_captions
from app.utils import display_filename, download_stem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

CHUNK = 1024 * 1024
ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
          422: {"model": ErrorResponse}}


class NotFound(CaptionForgeError):
    code = "not_found"
    http_status = 404


class NotReady(CaptionForgeError):
    code = "not_ready"
    http_status = 409


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _get_job(request: Request, job_id: str) -> dict:
    job = _jobs(request).get(job_id) if is_valid_job_id(job_id) else None
    if job is None:
        raise NotFound(f"Job '{job_id}' not found.")
    return job


def _result(job: dict, include_srt: bool = True) -> TranscriptionResult:
    segments = [CaptionSegment(**s) for s in job["segments"]] if job.get("segments") is not None else None
    srt = None
    if include_srt and segments is not None:
        srt = subtitles.to_srt(segments)  # CaptionSegment has the same fields as Caption
    return TranscriptionResult(
        job_id=job["job_id"], status=job["status"], stage=job.get("stage"),
        progress=job.get("progress", 0.0), filename=job.get("filename"),
        created_at=job.get("created_at"), finished_at=job.get("finished_at"),
        language=job.get("language"), language_probability=job.get("language_probability"),
        duration=job.get("duration"), has_video=job.get("has_video"),
        transcript=job.get("transcript"), segments=segments, srt_content=srt,
        edited=job.get("edited", False), model=job.get("model"),
        error=JobError(**job["error"]) if job.get("error") else None,
    )


# ── Health & config ──────────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request):
    """Returns 200 while the server runs; reports FFmpeg and model state."""
    mm = request.app.state.model_manager
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": "CaptionForge",
        "ffmpeg": audio_svc.ffmpeg_available(),
        "model": {"name": settings.whisper_model, "device": settings.whisper_device,
                  "active_device": getattr(mm, "active_device", None),
                  "loaded": getattr(mm, "loaded", False)},
    }


@router.get("/config")
async def config(request: Request):
    s = request.app.state.settings
    return {
        "supported_extensions": sorted(audio_svc.SUPPORTED_EXTENSIONS),
        "max_upload_mb": s.max_upload_mb,
        "max_duration_seconds": s.max_duration_seconds,
        "languages": transcription_svc.supported_languages(),
        "export_formats": list(subtitles.FORMATS),
        "model": s.whisper_model,
        "segmentation": {"max_chars_per_line": s.segmentation.max_chars_per_line,
                         "max_lines": s.segmentation.max_lines},
    }


# ── Upload / job creation ────────────────────────────────────────────────────

@router.post("/transcribe", status_code=202, response_model=CreateJobResponse,
             responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse},
                        422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
async def transcribe_file(
    request: Request,
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """
    Upload an audio/video file and start captioning. The file is validated
    (extension, size, real media content, presence of an audio track) before
    a job is created. Poll ``/api/status/{job_id}`` afterwards.
    """
    jobs = _jobs(request)
    settings = request.app.state.settings
    filename = display_filename(file.filename)

    if not audio_svc.is_supported(filename):
        raise CaptionForgeError(
            f"Unsupported file type '{os.path.splitext(filename)[1] or filename}'. "
            f"Supported: {', '.join(sorted(audio_svc.SUPPORTED_EXTENSIONS))}.",
            code="unsupported_format", http_status=415)

    lang, lang_error = transcription_svc.split_language(language)
    if lang_error:
        raise CaptionForgeError(lang_error, code="invalid_language", http_status=422)

    jobs.ensure_capacity()

    job_id = jobs.new_job_id()
    upload_dir = jobs.upload_dir(job_id)
    upload_dir.mkdir(parents=True, exist_ok=False)
    input_path = str(upload_dir / f"input{os.path.splitext(filename)[1].lower()}")
    try:
        size = await run_in_threadpool(_copy_limited, file.file, input_path,
                                       settings.max_upload_bytes)
        info = await run_in_threadpool(audio_svc.probe_media, input_path, 60)
        audio_svc.validate_media(info, settings.max_duration_seconds)
        jobs.submit(job_id, input_path, filename, lang, info.has_video)
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    logger.info("Job %s accepted: %s (%d bytes, %s)", job_id, filename, size, info.format_name)
    return CreateJobResponse(job_id=job_id, status=JobStatus.PENDING)


def _copy_limited(src, dest: str, limit: int) -> int:
    """Stream the spooled upload to disk in chunks, enforcing the size limit."""
    size = 0
    with open(dest, "xb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise CaptionForgeError(
                    f"File too large. Maximum allowed size is {limit // (1024 * 1024)} MB.",
                    code="file_too_large", http_status=413)
            out.write(chunk)
    if size == 0:
        raise MediaError("The uploaded file is empty.", code="empty_file")
    return size


# ── Status / results ─────────────────────────────────────────────────────────

@router.get("/status/{job_id}", response_model=JobStatusResponse, responses=ERRORS)
async def get_status(request: Request, job_id: str):
    job = _get_job(request, job_id)
    return JobStatusResponse(
        job_id=job_id, status=job["status"], stage=job.get("stage"),
        progress=job.get("progress", 0.0), queue_position=job.get("queue_position"),
        filename=job.get("filename"), created_at=job.get("created_at"),
        error=JobError(**job["error"]) if job.get("error") else None,
    )


@router.get("/result/{job_id}", response_model=TranscriptionResult, responses=ERRORS)
async def get_result(request: Request, job_id: str):
    """
    Current status plus, once ``status == "done"``, the captions, transcript
    and SRT text. Before completion the result fields are null.
    """
    return _result(_get_job(request, job_id))


@router.put("/result/{job_id}/segments", response_model=TranscriptionResult, responses=ERRORS)
async def update_segments(request: Request, job_id: str, body: UpdateCaptionsRequest):
    """Replace the captions of a finished job with an edited version."""
    job = _get_job(request, job_id)
    if job["status"] != JobStatus.DONE.value:
        raise NotReady("Captions can only be edited after the job has finished.")
    try:
        captions = normalize_edited_captions([s.model_dump() for s in body.segments])
    except CaptionValidationError as exc:
        raise CaptionForgeError(str(exc), code="invalid_captions", http_status=422)
    updated = await run_in_threadpool(_jobs(request).update_captions, job_id, captions)
    return _result(updated)


@router.get("/download/{job_id}", responses={**ERRORS, 200: {
    "content": {"application/x-subrip": {}, "text/vtt": {}}}})
async def download(request: Request, job_id: str,
                   format: str = Query("srt", pattern="^(srt|vtt)$")):
    """Download the captions as SubRip (.srt) or WebVTT (.vtt)."""
    job = _get_job(request, job_id)
    if job["status"] != JobStatus.DONE.value:
        raise NotReady("Transcription is not complete yet.")
    path = _jobs(request).subtitle_path(job_id, format)
    if not path.exists():
        raise NotFound("Subtitle file is missing on the server. Please process the file again.",
                       code="output_missing")
    return FileResponse(path=str(path), media_type=subtitles.FORMATS[format]["media_type"],
                        filename=f"{download_stem(job.get('filename'))}.{format}")


@router.delete("/jobs/{job_id}", status_code=204, responses=ERRORS)
async def delete_job(request: Request, job_id: str):
    """Delete a job and its files. A job that is processing is cancelled."""
    if not (is_valid_job_id(job_id) and await run_in_threadpool(_jobs(request).delete, job_id)):
        raise NotFound(f"Job '{job_id}' not found.")
    return Response(status_code=204)

"""
CaptionForge — FastAPI application entry point.

Startup (from backend/):
  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, load_settings
from app.errors import CaptionForgeError
from app.jobs import JobManager
from app.pipeline import run_pipeline
from app.services.audio import ffmpeg_available
from app.services.transcription import ModelManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("captionforge")

# Multipart framing adds a little on top of the file itself.
_MULTIPART_OVERHEAD = 1024 * 1024


def create_app(settings: Optional[Settings] = None, model_manager=None,
               pipeline: Callable = run_pipeline) -> FastAPI:
    settings = settings or load_settings()
    model_manager = model_manager or ModelManager(
        settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
        download_root=str(settings.model_dir) if settings.model_dir else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not ffmpeg_available():
            logger.error("FFmpeg/ffprobe not found on PATH — uploads will be rejected.")
        jobs = JobManager(settings, model_manager, pipeline)
        jobs.start()
        app.state.jobs = jobs
        logger.info("CaptionForge ready (model=%s, device=%s). API docs at /docs",
                    settings.whisper_model, settings.whisper_device)
        yield
        jobs.stop()

    app = FastAPI(
        title="CaptionForge",
        description="Automatic captions for audio/video using faster-whisper. Upload a file, "
                    "poll the job, review/edit captions and download SRT or WebVTT.",
        version="1.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.model_manager = model_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    @app.middleware("http")
    async def reject_oversized_uploads(request: Request, call_next):
        # Fail fast on declared-oversized bodies before they are spooled to disk.
        # Bodies without Content-Length are still capped while being copied.
        if request.method == "POST" and request.url.path == "/api/transcribe":
            length = request.headers.get("content-length")
            if length and length.isdigit() and \
                    int(length) > settings.max_upload_bytes + _MULTIPART_OVERHEAD:
                return JSONResponse(status_code=413, content={
                    "detail": f"File too large. Maximum allowed size is "
                              f"{settings.max_upload_mb} MB.",
                    "code": "file_too_large"})
        return await call_next(request)

    @app.exception_handler(CaptionForgeError)
    async def handle_app_error(request: Request, exc: CaptionForgeError):
        return JSONResponse(status_code=exc.http_status,
                            content={"detail": exc.message, "code": exc.code})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = f"{where}: {first.get('msg')}" if where else str(first.get("msg", "Invalid request"))
        return JSONResponse(status_code=422, content={
            "detail": msg, "code": "validation_error",
            "errors": [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in errors]})

    app.include_router(router)

    @app.get("/")
    async def root():
        return {"message": "CaptionForge API is running.", "docs": "/docs",
                "health": "/api/health"}

    return app


app = create_app()

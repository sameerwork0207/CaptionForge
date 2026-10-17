"""
The captioning pipeline for one job, independent of HTTP and job storage:

    media file → 16 kHz WAV (FFmpeg) → transcription (faster-whisper)
               → caption cues (segmentation) → SRT/VTT strings
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from app.config import Settings
from app.errors import JobCancelledError, MediaError
from app.services import audio as audio_svc
from app.services import captions as caption_svc
from app.services import transcription as transcription_svc
from app.services.captions import Caption

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    language: str
    language_probability: float
    duration: float
    transcript: str
    captions: List[Caption]


def run_pipeline(
    input_path: str,
    work_dir: str,
    settings: Settings,
    model_manager: transcription_svc.ModelManager,
    *,
    language: Optional[str] = None,
    on_stage: Callable[[str], None] = lambda stage: None,
    on_progress: Callable[[float], None] = lambda frac: None,
    should_cancel: Callable[[], bool] = lambda: False,
) -> PipelineResult:
    on_stage("extracting")
    wav_path = os.path.join(work_dir, "audio.wav")
    audio_svc.extract_audio(input_path, wav_path, timeout=settings.ffmpeg_timeout_seconds)
    duration = audio_svc.wav_duration(wav_path)
    if duration > settings.max_duration_seconds:
        # Containers that do not report duration are only checked here.
        raise MediaError(audio_svc.too_long_message(duration, settings.max_duration_seconds),
                         code="media_too_long")
    if should_cancel():
        raise JobCancelledError("The job was cancelled.")

    on_stage("transcribing")
    out = transcription_svc.transcribe(
        model_manager, wav_path,
        language=language,
        beam_size=settings.whisper_beam_size,
        duration=duration,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )

    on_stage("segmenting")
    captions = caption_svc.build_captions(out.segments, settings.segmentation)
    return PipelineResult(
        language=out.language,
        language_probability=out.language_probability,
        duration=round(duration, 3),
        transcript=transcription_svc.transcript_text(out.segments),
        captions=captions,
    )

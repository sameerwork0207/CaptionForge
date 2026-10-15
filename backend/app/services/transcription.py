"""
Transcription service using faster-whisper (CTranslate2 Whisper).

* The model is loaded once and reused (guarded by a lock so two concurrent
  first requests cannot load it twice).
* ``WHISPER_DEVICE=auto`` tries CUDA with a tiny test inference and falls back
  to CPU if the CUDA runtime libraries are missing.
* Word-level timestamps are requested so the caption segmenter can split on
  real word boundaries. These are Whisper's estimates (cross-attention
  alignment), typically accurate to a few hundred milliseconds, not exact.
* Failures are mapped to typed errors with actionable messages.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np

from app.errors import JobCancelledError, ModelUnavailableError, TranscriptionError

logger = logging.getLogger(__name__)


@dataclass
class Word:
    text: str        # Whisper token text, including its leading space if any
    start: float
    end: float


@dataclass
class RawSegment:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)


@dataclass
class TranscriptionOutput:
    language: str
    language_probability: float
    segments: List[RawSegment]


def supported_languages() -> List[str]:
    from faster_whisper.tokenizer import _LANGUAGE_CODES
    return sorted(_LANGUAGE_CODES)


_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)

# CTranslate2 hangs if CUDA initialisation is attempted again after it has
# failed once in the same process (verified with missing cuBLAS on Windows),
# so a CUDA failure is remembered for the lifetime of the process.
_cuda_failure: Optional[str] = None
_cuda_lock = threading.Lock()


# ── Model management ─────────────────────────────────────────────────────────

class ModelManager:
    def __init__(self, model_name: str, device: str, compute_type: str,
                 download_root: Optional[str] = None):
        self.model_name = model_name
        self.requested_device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.active_device: Optional[str] = None
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model=%s device=%s compute=%s",
                    self.model_name, device, compute_type)
        return WhisperModel(self.model_name, device=device, compute_type=compute_type,
                            download_root=self.download_root)

    @staticmethod
    def _smoke_test(model) -> None:
        """Run a 1-second inference; CUDA library problems only surface here."""
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en",
                                       beam_size=1, vad_filter=False)
        list(segments)

    def _load_cuda(self, compute_type: str):
        """Load on CUDA and verify with a test inference; failures are sticky."""
        global _cuda_failure
        with _cuda_lock:
            if _cuda_failure is not None:
                raise RuntimeError(_cuda_failure)
            try:
                model = self._load("cuda", compute_type)
                self._smoke_test(model)
                return model
            except Exception as exc:
                _cuda_failure = f"CUDA initialisation failed earlier: {exc}"
                raise

    def get(self):
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                if self.requested_device == "auto":
                    try:
                        compute = "float16" if self.compute_type == "default" else self.compute_type
                        model = self._load_cuda(compute)
                        self.active_device = "cuda"
                    except Exception as exc:  # noqa: BLE001 - any CUDA failure → CPU
                        logger.warning("CUDA unavailable (%s); falling back to CPU.", exc)
                        compute = "int8" if self.compute_type in ("default", "float16") else self.compute_type
                        model = self._load("cpu", compute)
                        self.active_device = "cpu"
                elif self.requested_device == "cuda":
                    model = self._load_cuda(self.compute_type)
                    self.active_device = "cuda"
                else:
                    model = self._load(self.requested_device, self.compute_type)
                    self.active_device = self.requested_device
            except Exception as exc:
                raise _map_model_error(exc, self.model_name) from exc
            self._model = model
            logger.info("Model loaded on %s.", self.active_device)
            return model


def _map_model_error(exc: Exception, model_name: str) -> ModelUnavailableError:
    text = str(exc)
    logger.error("Model load failed: %r", exc)
    if "cublas" in text.lower() or "cudnn" in text.lower() or "cuda" in text.lower():
        return ModelUnavailableError(
            "GPU inference is not available (CUDA runtime libraries missing or "
            "incompatible). Set WHISPER_DEVICE=cpu or WHISPER_DEVICE=auto.")
    if "invalid model size" in text.lower():
        return ModelUnavailableError(f"Unknown Whisper model '{model_name}'. "
                                     "Use e.g. tiny, base, small, medium, large-v3.")
    if isinstance(exc, MemoryError) or "out of memory" in text.lower():
        return ModelUnavailableError(
            f"Not enough memory to load model '{model_name}'. Try a smaller model.")
    return ModelUnavailableError(
        f"Could not load Whisper model '{model_name}'. On first use the model is "
        "downloaded from Hugging Face, which requires internet access.")


# ── Transcription ────────────────────────────────────────────────────────────

def transcribe(
    model_manager: ModelManager,
    audio_path: str,
    *,
    language: Optional[str] = None,
    beam_size: int = 5,
    duration: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> TranscriptionOutput:
    """
    Transcribe a 16 kHz mono WAV file.

    ``on_progress`` receives a 0..1 fraction based on how far into the audio
    the decoder has reached. ``should_cancel`` is checked after every segment.
    """
    model = model_manager.get()
    logger.info("Transcribing %s (language=%s)", audio_path, language or "auto")

    try:
        segments_iter, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        segments: List[RawSegment] = []
        for seg in segments_iter:
            if should_cancel and should_cancel():
                raise JobCancelledError("The job was cancelled.")
            text = seg.text.strip()
            if on_progress and duration:
                on_progress(min(1.0, float(seg.end) / duration))
            # VAD removes most silence hallucinations; drop leftover segments
            # that contain no letters/digits at all (e.g. " ...").
            if not text or _PUNCT_ONLY.match(text):
                continue
            words = [Word(text=w.word, start=float(w.start), end=float(w.end))
                     for w in (seg.words or []) if w.word.strip()]
            segments.append(RawSegment(start=float(seg.start), end=float(seg.end),
                                       text=text, words=words))
    except (JobCancelledError, ModelUnavailableError):
        raise
    except MemoryError as exc:
        raise TranscriptionError("Ran out of memory during transcription. Try a smaller "
                                 "model or a shorter file.", code="out_of_memory") from exc
    except RuntimeError as exc:
        if "cublas" in str(exc).lower() or "cudnn" in str(exc).lower():
            raise _map_model_error(exc, model_manager.model_name) from exc
        logger.exception("Transcription failed")
        raise TranscriptionError("Speech recognition failed while processing this file.") from exc

    logger.info("Transcription complete: language=%s (p=%.2f), %d segments",
                info.language, info.language_probability, len(segments))
    return TranscriptionOutput(language=info.language,
                               language_probability=float(info.language_probability),
                               segments=segments)


def transcript_text(segments: List[RawSegment]) -> str:
    return " ".join(s.text for s in segments).strip()


def split_language(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Normalise a user language option. Returns (language_or_None, error)."""
    if value is None or value.strip() == "" or value.strip().lower() == "auto":
        return None, None
    code = value.strip().lower()
    if code not in supported_languages():
        return None, f"Unsupported language code '{value}'."
    return code, None

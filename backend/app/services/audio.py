"""
Media inspection and audio extraction using FFmpeg / ffprobe.

* ``probe_media`` inspects the real content of an upload (not its extension).
* ``validate_media`` turns probe results into user-facing errors.
* ``extract_audio`` converts the first audio stream to 16 kHz mono PCM WAV,
  the format faster-whisper expects.

Raw FFmpeg output is logged server-side only; clients get short, actionable
messages without filesystem paths.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass
from typing import Optional

from app.errors import FFmpegUnavailableError, MediaError

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass
class MediaInfo:
    format_name: str
    duration: Optional[float]      # seconds; None if the container does not report it
    has_audio: bool
    has_video: bool
    audio_codec: Optional[str]


def is_supported(filename: str) -> bool:
    """Return True if the file extension is in the allow-list."""
    return os.path.splitext(filename)[1].lower() in SUPPORTED_EXTENSIONS


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise FFmpegUnavailableError(
            "FFmpeg/ffprobe is not installed on the server or not on PATH."
        )
    except subprocess.TimeoutExpired:
        raise MediaError(
            f"Media processing timed out after {timeout} seconds.", code="media_timeout"
        )


def probe_media(path: str, timeout: int = 60) -> MediaInfo:
    """Inspect a media file with ffprobe. Raises MediaError if it is unreadable."""
    if os.path.getsize(path) == 0:
        raise MediaError("The uploaded file is empty.", code="empty_file")

    result = _run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        timeout,
    )
    if result.returncode != 0:
        logger.warning("ffprobe failed for %s: %s", path,
                       result.stderr.decode("utf-8", "replace")[-2000:])
        raise MediaError(
            "The file could not be read as audio or video. It may be corrupt or "
            "not a real media file.",
            code="corrupt_media",
        )

    try:
        data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise MediaError("The file could not be read as audio or video.", code="corrupt_media")

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    # Cover art in MP3/M4A shows up as a single-frame video stream; ignore it.
    video = [s for s in streams if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")]

    duration = None
    try:
        duration = float(fmt["duration"])
    except (KeyError, TypeError, ValueError):
        pass

    return MediaInfo(
        format_name=fmt.get("format_name", "unknown"),
        duration=duration,
        has_audio=bool(audio),
        has_video=bool(video),
        audio_codec=audio[0].get("codec_name") if audio else None,
    )


def format_duration(seconds: float) -> str:
    """Human-readable duration for messages: 10 → "10 seconds", 5400 → "90 minutes"."""
    if seconds < 120:
        return f"{seconds:.1f} seconds".replace(".0 ", " ")
    return f"{seconds / 60:.1f} minutes".replace(".0 ", " ")


def too_long_message(duration: float, limit: float) -> str:
    return (f"The media is {format_duration(duration)} long; "
            f"the maximum is {format_duration(limit)}.")


def validate_media(info: MediaInfo, max_duration_seconds: int) -> None:
    """Raise MediaError if probed media cannot be captioned."""
    if not info.has_audio:
        raise MediaError(
            "This file has no audio track, so there is nothing to transcribe.",
            code="no_audio_stream",
        )
    if not info.audio_codec or info.audio_codec == "none":
        raise MediaError(
            "The audio in this file uses a codec that cannot be decoded.",
            code="unsupported_codec",
        )
    if info.duration is not None and info.duration > max_duration_seconds:
        raise MediaError(
            too_long_message(info.duration, max_duration_seconds),
            code="media_too_long",
        )


def extract_audio(input_path: str, output_path: str, timeout: int = 600) -> str:
    """Convert the first audio stream to 16 kHz mono 16-bit PCM WAV."""
    logger.info("Extracting audio: %s -> %s", input_path, output_path)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-map", "0:a:0",          # first audio stream only
        "-vn", "-sn", "-dn",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path,
    ]
    result = _run(cmd, timeout)
    if result.returncode != 0:
        logger.warning("ffmpeg failed (code %s) for %s: %s", result.returncode, input_path,
                       result.stderr.decode("utf-8", "replace")[-2000:])
        raise MediaError(
            "Could not decode the audio in this file. It may be corrupt or use an "
            "unsupported codec.",
            code="decode_failed",
        )
    if not os.path.exists(output_path) or wav_duration(output_path) <= 0:
        raise MediaError("The file contains no decodable audio.", code="decode_failed")
    return output_path


def wav_duration(path: str) -> float:
    """Exact duration of a PCM WAV file in seconds (read from its header)."""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        return wf.getnframes() / float(rate) if rate else 0.0

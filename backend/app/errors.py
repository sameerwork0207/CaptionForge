"""
Typed application errors.

Every failure the user can act on is raised as a ``CaptionForgeError`` with a
stable machine-readable ``code`` and a human-readable ``message`` that is safe
to show to clients (no server paths, no raw tool output). Technical details go
to the server log instead.
"""

from __future__ import annotations


class CaptionForgeError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status


class MediaError(CaptionForgeError):
    """The uploaded media is unusable (empty, corrupt, no audio, too long...)."""
    code = "invalid_media"
    http_status = 422


class FFmpegUnavailableError(CaptionForgeError):
    code = "ffmpeg_unavailable"
    http_status = 503


class TranscriptionError(CaptionForgeError):
    code = "transcription_failed"
    http_status = 500


class ModelUnavailableError(TranscriptionError):
    code = "model_unavailable"
    http_status = 503


class JobCancelledError(CaptionForgeError):
    code = "cancelled"
    http_status = 409

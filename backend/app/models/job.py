"""
Pydantic schemas for the CaptionForge API.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Job lifecycle: pending → processing → done | error."""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class CaptionSegment(BaseModel):
    """A single caption cue."""
    index: int
    start: float          # seconds
    end: float            # seconds
    text: str             # 1..N lines separated by "\n"


class JobError(BaseModel):
    code: str
    message: str


class JobStatusResponse(BaseModel):
    """Lightweight status for polling."""
    job_id: str
    status: JobStatus
    stage: Optional[str] = None           # queued | extracting | transcribing | segmenting | exporting
    progress: float = 0.0                 # 0..1, based on audio position decoded
    queue_position: Optional[int] = None  # 1-based while pending
    filename: Optional[str] = None
    created_at: Optional[str] = None
    error: Optional[JobError] = None


class TranscriptionResult(BaseModel):
    """Full job result (status fields plus captions once done)."""
    job_id: str
    status: JobStatus
    stage: Optional[str] = None
    progress: float = 0.0
    filename: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    language: Optional[str] = None
    language_probability: Optional[float] = None
    duration: Optional[float] = None
    has_video: Optional[bool] = None
    transcript: Optional[str] = None
    segments: Optional[List[CaptionSegment]] = None
    srt_content: Optional[str] = None
    edited: bool = False
    model: Optional[str] = None
    error: Optional[JobError] = None


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class CaptionEdit(BaseModel):
    start: float = Field(..., ge=0)
    end: float = Field(..., ge=0)
    text: str = Field(..., max_length=1000)


class UpdateCaptionsRequest(BaseModel):
    segments: List[CaptionEdit] = Field(..., max_length=20000)


class ErrorResponse(BaseModel):
    detail: str
    code: str

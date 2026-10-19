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
    id: Optional[str] = None   # stable within a job; never reused for another caption


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
    revision: Optional[int] = None        # 1 when finished; +1 on every saved edit
    model: Optional[str] = None
    error: Optional[JobError] = None


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus


MAX_CAPTION_TIME = 359_999.0   # seconds (99:59:59), the largest two-digit-hour timestamp


class CaptionEdit(BaseModel):
    # Python's JSON parser accepts NaN/Infinity literals, so reject them explicitly.
    start: float = Field(..., ge=0, le=MAX_CAPTION_TIME, allow_inf_nan=False)
    end: float = Field(..., ge=0, le=MAX_CAPTION_TIME, allow_inf_nan=False)
    text: str = Field(..., max_length=1000)
    # Id of an existing caption, as returned by the server. Omit for a new caption.
    id: Optional[str] = Field(None, min_length=1, max_length=64)


class UpdateCaptionsRequest(BaseModel):
    segments: List[CaptionEdit] = Field(..., max_length=20000)
    # If given, the save is rejected (409) unless it equals the job's current revision.
    base_revision: Optional[int] = Field(None, ge=1)


class ErrorResponse(BaseModel):
    detail: str
    code: str

"""
Application configuration.

All settings come from environment variables (optionally loaded from
``backend/.env``) so nothing machine-specific lives in source code.
See ``backend/.env.example`` for the full list with defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]

VALID_DEVICES = {"cpu", "cuda", "auto"}


class ConfigError(ValueError):
    """Raised when an environment variable holds an invalid value."""


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real env vars take precedence."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(env: dict, name: str, default: int, minimum: int = 0,
         maximum: Optional[int] = None) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _float(env: dict, name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _path(env: dict, name: str, default: Path) -> Path:
    raw = env.get(name)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (BACKEND_DIR / p)


@dataclass(frozen=True)
class SegmentationConfig:
    """Rules used to turn timed words into readable subtitle cues."""
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_cps: float = 17.0          # reading speed target (characters / second)
    min_duration: float = 1.0      # seconds a cue should stay on screen
    max_duration: float = 7.0      # seconds before a cue is forced to split
    pause_split: float = 0.6       # silence (s) between words that forces a new cue
    min_gap: float = 0.05          # minimum gap kept between consecutive cues


@dataclass(frozen=True)
class Settings:
    upload_dir: Path
    output_dir: Path
    max_upload_mb: int
    max_duration_seconds: int
    ffmpeg_timeout_seconds: int
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_beam_size: int
    model_dir: Optional[Path]
    job_retention_hours: float
    max_queued_jobs: int
    cors_origins: List[str]
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings(env: Optional[dict] = None) -> Settings:
    """Build Settings from ``env`` (defaults to os.environ after loading .env)."""
    if env is None:
        _load_dotenv(BACKEND_DIR / ".env")
        env = dict(os.environ)

    device = env.get("WHISPER_DEVICE", "cpu").strip().lower()
    if device not in VALID_DEVICES:
        raise ConfigError(f"WHISPER_DEVICE must be one of {sorted(VALID_DEVICES)}, got {device!r}")

    default_compute = "int8" if device == "cpu" else "default"
    model_dir = env.get("WHISPER_MODEL_DIR")

    seg = SegmentationConfig(
        max_chars_per_line=_int(env, "CAPTION_MAX_CHARS_PER_LINE", 42, minimum=10),
        max_lines=_int(env, "CAPTION_MAX_LINES", 2, minimum=1),
        max_cps=_float(env, "CAPTION_MAX_CPS", 17.0, minimum=1.0),
        min_duration=_float(env, "CAPTION_MIN_DURATION", 1.0, minimum=0.1),
        max_duration=_float(env, "CAPTION_MAX_DURATION", 7.0, minimum=1.0),
        pause_split=_float(env, "CAPTION_PAUSE_SPLIT", 0.6, minimum=0.0),
    )
    if seg.min_duration >= seg.max_duration:
        raise ConfigError("CAPTION_MIN_DURATION must be smaller than CAPTION_MAX_DURATION")

    origins = [o.strip() for o in env.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()]

    return Settings(
        upload_dir=_path(env, "UPLOAD_DIR", BACKEND_DIR / "uploads"),
        output_dir=_path(env, "OUTPUT_DIR", BACKEND_DIR / "outputs"),
        max_upload_mb=_int(env, "MAX_UPLOAD_MB", 500, minimum=1),
        max_duration_seconds=_int(env, "MAX_DURATION_SECONDS", 3 * 3600, minimum=1,
                                  maximum=99 * 3600),
        ffmpeg_timeout_seconds=_int(env, "FFMPEG_TIMEOUT_SECONDS", 600, minimum=5),
        whisper_model=env.get("WHISPER_MODEL", "base").strip(),
        whisper_device=device,
        whisper_compute_type=env.get("WHISPER_COMPUTE_TYPE", default_compute).strip(),
        whisper_beam_size=_int(env, "WHISPER_BEAM_SIZE", 5, minimum=1),
        model_dir=Path(model_dir) if model_dir else None,
        job_retention_hours=_float(env, "JOB_RETENTION_HOURS", 24.0, minimum=0.0),
        max_queued_jobs=_int(env, "MAX_QUEUED_JOBS", 20, minimum=1),
        cors_origins=origins,
        segmentation=seg,
    )

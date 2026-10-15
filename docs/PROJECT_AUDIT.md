# CaptionForge — Project Audit

Audit date: 2026-10-15. This records the repository **as found**, before any changes were made.

Legend: **[V]** = verified by running code or reading the implementation · **[A]** = assumption / inference not directly verified.

---

## 1. Current architecture

```
CaptionForge/
└── backend/
    ├── requirements.txt
    ├── test_pipeline.py          # standalone script, not pytest
    ├── uploads/  outputs/        # runtime dirs (.gitkeep only)
    └── app/
        ├── main.py               # FastAPI app, CORS, router include
        ├── state.py              # in-memory dict job store + lock
        ├── api/routes.py         # all endpoints + pipeline orchestration
        ├── models/job.py         # Pydantic schemas, JobStatus enum
        └── services/
            ├── audio.py          # extension check, ffmpeg extract, ffprobe duration
            ├── transcription.py  # faster-whisper singleton + transcribe()
            ├── captions.py       # segment merging + 2-line wrap
            └── srt.py            # SRT formatting / writing
```

- **[V]** Single FastAPI process. `POST /api/transcribe` reads the whole upload into memory, writes it to `uploads/<uuid><ext>`, and schedules `_run_pipeline` via Starlette `BackgroundTasks`, which runs in the threadpool.
- **[V]** Pipeline: ffmpeg → 16 kHz mono WAV → faster-whisper (`base`, CPU, int8, VAD on, beam 5) → caption merge → SRT written to `outputs/<uuid>.srt` → result stored in the in-memory dict.
- **[V]** There is **no frontend** in the repository. CORS defaults to `http://localhost:3000`, which suggests a Next.js dev server was planned **[A]**.
- **[V]** It is not a Git repository, so there is no change history to preserve or inspect.

## 2. Implemented features

| Feature | State |
|---|---|
| `GET /api/health` | **[V]** works, static response |
| `POST /api/transcribe` (multipart `file`) | **[V]** works for valid media |
| `GET /api/result/{job_id}` (poll) | **[V]** works, 404 for unknown job |
| `GET /api/download/{job_id}` (SRT) | **[V]** 404 unknown / 409 not ready / file response |
| Extension allow-list (mp4 mov mkv wav mp3 m4a webm) | **[V]** |
| FFmpeg extraction to 16 kHz mono PCM, 300 s timeout | **[V]** |
| faster-whisper model reused across requests (lazy singleton) | **[V]** |
| VAD filtering | **[V]** |
| SRT export | **[V]** (with bugs, see §4) |
| Upload + temp WAV deleted after processing | **[V]** |

## 3. Incomplete / missing features

- No frontend: no upload UI, progress display, caption review/editing, or download UI. **[V]**
- No WebVTT export. **[V]**
- No caption editing API. **[V]**
- No language selection exposed through the API, although `transcribe()` accepts one. **[V]**
- Media content is not validated. Only the extension is checked, with no ffprobe of streams, duration, or audio presence. **[V]**
- Word-level timestamps are not used; captions follow Whisper segment granularity. **[V]**
- No progress reporting beyond `pending`/`processing`. **[V]**
- No job cancellation or deletion, and no retention/cleanup of `outputs/`. **[V]**
- No pytest suite, README, `.env.example`, or documentation. **[V]**

## 4. Bugs and risks

| # | Severity | Finding | Evidence |
|---|---|---|---|
| B1 | **Critical** | **Silent/no-speech audio crashes transcription.** faster-whisper 1.0.3 with auto language detection raises `ValueError: max() arg is an empty sequence` when VAD removes all audio. The existing `test_pipeline.py` fails on this. | **[V]** ran `python test_pipeline.py` and it failed at the transcription step |
| B2 | High | The entire upload is read into memory (`await file.read()`) before the 500 MB size check runs. A large upload can exhaust RAM. | **[V]** code |
| B3 | High | Raw FFmpeg stderr is returned to the client as the job error: a multi-KB build banner plus absolute server paths. The error is neither actionable nor safe to expose. | **[V]** uploaded an empty `.mp3` and the job error contained the full ffmpeg banner and `C:\Projects\...` path |
| B4 | High | Empty, corrupt, and no-audio files are accepted (HTTP 200) and fail only later in the background job. | **[V]** empty file → 200 then `error` |
| B5 | Medium | Timestamp rounding bug: `_format_timestamp(59.9996)` → `00:00:59,1000`, which is invalid SRT. | **[V]** |
| B6 | Medium | Segmentation does not enforce its limits. A single Whisper segment over 84 chars produces line 2 with 85 chars, and text is never split across captions. | **[V]** |
| B7 | Medium | After a sentence flush, the next caption's start is set to the *previous segment's end*, so captions start early across pauses (example: a caption at 2 s → 12 s for speech at 10–12 s). | **[V]** |
| B8 | Medium | `MIN_DURATION` extension can overlap the next caption, and adjacent short segments are merged regardless of pauses. There are no max-duration or reading-speed rules. | **[V]** code/inspection |
| B9 | Medium | Concurrent jobs run in parallel threadpool threads against one CPU model, so CPU contention slows them all. The lazy model load has no lock, so two first requests can load the model twice. | **[V]** code; impact **[A]** |
| B10 | Medium | The job store is in-memory only. All jobs are lost on restart, the dict grows without bound, and `outputs/*.srt` is never deleted. | **[V]** code |
| B11 | Low | `get_audio_duration` swallows all errors and returns `0.0`. | **[V]** |
| B12 | Low | `@app.on_event("startup")` is deprecated in current FastAPI. | **[V]** |
| B13 | Low | The model-load failure (no network, bad model name, missing CUDA libs) is surfaced as a raw exception string. | **[V]** code |
| R1 | Info | Path traversal: upload paths are `<uuid><whitelisted ext>`, and download looks the job up before building a path. **No traversal found.** | **[V]** |

## 5. Dependency and compatibility issues

Machine (verified): Windows 11, i7-13700HX (24 threads), 15.7 GB RAM, RTX 4050 Laptop 6 GB, Python 3.11.9 (global interpreter only, **no venv**), FFmpeg 9.0.2 on PATH, Node 24.12 / npm 11.6. The `Systran/faster-whisper-base` model (142 MB) is already in the HF cache. CTranslate2 reports one CUDA device. Whether the CUDA runtime libraries (cuBLAS/cuDNN) are present has **not been verified** **[A]**.

| Package | requirements.txt | Installed globally | Note |
|---|---|---|---|
| fastapi | 0.115.0 | 0.115.6 | pins not reproducible against the actual env |
| uvicorn | 0.30.6 | 0.34.0 | |
| python-multipart | 0.0.9 | 0.0.20 | |
| faster-whisper | 1.0.3 | 1.0.3 | **has the silent-audio bug B1** |
| torch / torchaudio | 2.4.1 / 2.4.1 | 2.14.0 / not installed | **unused**. faster-whisper does not need torch; this is a multi-GB install for nothing |
| ffmpeg-python | 0.2.0 | 0.2.0 | **unused**. Code calls the `ffmpeg` binary via `subprocess` |
| aiofiles | 23.2.1 | 23.2.1 | **unused** |
| pydantic | 2.8.2 | 2.10.4 | |

Pytest and httpx are installed globally but are not declared.

## 6. Existing tests and coverage

- `backend/test_pipeline.py` is a script, not a pytest suite. It covers `is_supported`, ffmpeg extraction of a WAV, a transcription call on a **sine wave** (no speech), segmentation on **hard-coded** segments, and 3 timestamp cases.
- **Result: FAIL** (B1). No test exercises real speech, the API, error paths, or export validity.

## 7. Required changes (priority order)

1. **P0** Fix B1 (silent audio). Isolate dependencies in a venv and drop the unused torch, torchaudio, ffmpeg-python, and aiofiles.
2. **P0** Validate uploads up front: streamed write with a size cap, ffprobe content and audio-stream check, duration limit, and clean user-facing errors (B2, B3, B4, B11).
3. **P0** Fix timestamp formatting (B5) and rewrite segmentation with enforced limits, word timestamps, pauses, reading speed, and non-overlap (B6–B8).
4. **P1** Job manager: a serial worker queue, a model-load lock, real progress, cancellation and deletion, on-disk job records with a retention policy, and documented restart behaviour (B9, B10).
5. **P1** WebVTT export, a caption edit API, and download filenames and content types.
6. **P1** Frontend: upload → progress → review/edit → download, wired to the real backend.
7. **P1** pytest suite (unit, API, real-model, E2E) plus docs (README, ARCHITECTURE, TESTING, `.env.example`).
8. **P2** Configuration module (env-driven), health endpoint reporting ffmpeg and model state, lifespan instead of `on_event`.

## 8. Commands for running the current (as-found) application

```powershell
cd backend
pip install -r requirements.txt      # note: pulls an unused multi-GB torch
uvicorn app.main:app --reload --port 8000
python test_pipeline.py              # currently FAILS (B1)
```

---

## 9. Resolution status (after implementation, 2026-10-15)

| Item | Status | Where |
|---|---|---|
| B1 silent-audio crash | **Fixed**: faster-whisper 1.1.1, verified with real model | `requirements.txt`, `test_model_pipeline.py` |
| B2 upload read into memory | **Fixed**: Content-Length pre-check + chunked copy with cap | `main.py`, `api/routes.py` |
| B3 raw FFmpeg stderr / paths leaked | **Fixed**: typed errors with codes; stderr logged server-side only | `errors.py`, `services/audio.py` |
| B4 bad files accepted | **Fixed**: ffprobe validation before a job is created | `services/audio.py` |
| B5 timestamp rounding | **Fixed** | `services/subtitles.py` |
| B6–B8 segmentation | **Rewritten**: word-level, limits enforced, no overlaps | `services/captions.py` |
| B9 parallel jobs / load race | **Fixed**: single worker queue, locked model load | `jobs.py`, `services/transcription.py` |
| B10 volatile, unbounded state | **Fixed**: JSON persistence, retention purge, delete endpoint | `jobs.py` |
| B11 duration errors swallowed | **Fixed**: duration read from the extracted WAV header | `services/audio.py`, `pipeline.py` |
| B12 deprecated startup hook | **Fixed**: lifespan | `main.py` |
| B13 raw model errors | **Fixed**: actionable `model_unavailable` messages | `services/transcription.py` |
| New: repeated CUDA init hangs | **Found during verification and fixed** | `services/transcription.py` |
| Unused deps (torch, torchaudio, ffmpeg-python, aiofiles) | **Removed** | `requirements.txt` |
| Frontend | **Built**: Next.js upload → progress → edit → download | `frontend/` |
| WebVTT, caption editing API, language option | **Added** | `api/routes.py` |
| Tests | 144 backend tests (unit, API, real-model, HTTP + browser E2E), 6 frontend tests | `docs/TESTING.md` |

Removed files: `backend/test_pipeline.py` (ported into `tests/`), `backend/app/state.py` (replaced by `jobs.py`),
`backend/app/services/srt.py` (replaced by `subtitles.py`).

API compatibility: `POST /api/transcribe`, `GET /api/result/{id}` and `GET /api/download/{id}` keep their paths,
and the status values `pending/processing/done/error` are unchanged. Changes: `transcribe` now returns **202**
instead of 200; `/result` adds fields; error bodies add `code`; `error` in results is now `{code, message}` instead
of a string. No client existed, so nothing else was affected.

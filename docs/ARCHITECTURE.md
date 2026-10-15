# CaptionForge — Architecture

## Overview

```
 Browser (Next.js UI) ── HTTP/JSON + multipart ──►  FastAPI (single process)
   upload (XHR, progress)                            │
   poll /api/status every 1 s                        ├─ api/routes.py      thin HTTP layer, validation, error mapping
   edit captions (PUT)                               ├─ jobs.py            queue, 1 worker thread, persistence, cleanup
   download .srt/.vtt                                ├─ pipeline.py        one job: extract → transcribe → segment
                                                     └─ services/
                                                         ├─ audio.py          ffprobe validation, ffmpeg → 16 kHz mono WAV
                                                         ├─ transcription.py  faster-whisper model manager + transcribe
                                                         ├─ captions.py       deterministic segmentation, edit validation
                                                         └─ subtitles.py      SRT / WebVTT rendering
   storage:  uploads/<job_id>/   (temporary)       outputs/<job_id>/{job.json, captions.srt, captions.vtt}
```

### Request flow
1. `POST /api/transcribe`. The handler checks the extension allow-list and the language code, and checks queue
   capacity. It then streams the upload to `uploads/<uuid>/input.<ext>` with a size cap and runs **ffprobe**
   (content, audio stream, duration). Any failure returns 4xx/503 immediately and removes the files. On success
   the job is queued and the handler returns **202** with the job id.
2. The worker thread runs `pipeline.run_pipeline`:
   1. FFmpeg extracts the first audio stream to 16 kHz mono PCM WAV. The exact duration is read from the WAV
      header.
   2. faster-whisper transcribes with VAD, beam 5 and word timestamps. Progress is the decoded audio position
      divided by the duration.
   3. Segmentation builds the cues.
   4. SRT and VTT files are written atomically.
3. The client polls `/api/status`, then fetches `/api/result`. Edits are sent with `PUT .../segments`, which
   re-validates and rewrites both export files. Downloads stream the stored files.

## Components and decisions

### Single process, in-process worker (no Celery/Redis)
**Problem:** transcription is CPU-heavy and takes seconds to minutes, so it cannot run inside the request.
The original code used Starlette `BackgroundTasks`, which runs jobs in parallel threadpool threads.
**Decision:** one dedicated worker thread with a FIFO queue (`MAX_QUEUED_JOBS` bound).
**Why:** Whisper on CPU already saturates all cores, so parallel jobs just contend. One worker gives predictable
latency and memory use, since only one model is in RAM. Celery/Redis or a broker would add infrastructure with no
benefit on one machine. **Trade-off:** no horizontal scaling. Running uvicorn with `--workers N` would create N
independent queues and models, so it is not supported.

### Job state: in memory + JSON on disk (no database)
Each status change writes `outputs/<id>/job.json` atomically (temp file + `os.replace`). At startup, finished
jobs are reloaded. Jobs that were pending or processing are marked `error/interrupted`, because their upload is
gone and resuming is not attempted, and orphaned upload directories are removed. Only names that are valid job
UUIDs are ever deleted.
**Alternatives:** SQLite would add schema and migrations for a handful of fields; a pure in-memory dict
(original) loses everything on restart. Neither was justified for this scope.

### Media handling: FFmpeg via `subprocess` (removed `ffmpeg-python`)
Direct argument lists (no shell), `-nostdin`, timeouts, `-map 0:a:0`. ffprobe JSON is used for validation,
and attached cover art is not counted as video. FFmpeg's stderr is logged server-side only. Clients receive
short messages with stable codes, which fixes the original leak of the FFmpeg banner and server paths.
`ffmpeg-python` was unused and is unmaintained, so it was dropped.

### Speech recognition: faster-whisper 1.1.1 (upgraded from 1.0.3)
- **Why faster-whisper:** CTranslate2 int8 inference is fast on CPU, has no PyTorch dependency, and gives the
  same Whisper accuracy. No training is needed; pretrained multilingual models are sufficient.
- **Why the upgrade:** 1.0.3 crashes on audio where VAD finds no speech
  (`ValueError: max() arg is an empty sequence`), which the original test reproduced. 1.1.1 handles it.
  `requests` is now declared explicitly because 1.1.1 imports it.
- **Removed torch/torchaudio:** they were never imported and are a multi-GB install.
- **Model manager:** the model is loaded lazily once and guarded by a lock. `auto` mode loads on CUDA, runs a 1 s
  test inference (missing cuBLAS/cuDNN only fails at inference time), and falls back to CPU. A CUDA failure is
  **remembered for the process**, because a second CUDA initialisation after a failure hangs CTranslate2
  (verified on this machine). Errors are mapped to actionable `model_unavailable`, `out_of_memory` and
  `transcription_failed` codes.
- **VAD on:** without it, Whisper produced invented text on silence ("You") in testing. Segments containing only
  punctuation are dropped as well.
- **Default model `base` on CPU int8:** CUDA libraries are not installed on the target machine and cannot be
  assumed.

### Caption segmentation (`services/captions.py`)
Deterministic and configurable (`SegmentationConfig`). It works on **word tokens** from Whisper word
timestamps; if those are missing, word times are estimated proportionally to word length.
- A new cue starts when there is a pause ≥ 0.6 s, a sentence end once the cue has ≥ ½ line of text, or the
  text no longer fits in 2 × 42 characters, or the cue would exceed 7 s. On overflow, a break after a clause
  comma in the second half of the cue is preferred.
- Lines are balanced across two lines. Text without spaces (e.g. CJK) is split by characters.
- Timing: the end time is extended toward ≥ 1 s and ≤ 17 characters per second, but never closer than 50 ms to
  the next cue. A final pass guarantees monotonic, non-overlapping, strictly positive cues at millisecond
  precision.
- Tokens keep Whisper's own spacing, so languages without spaces are not corrupted.
- No LLM is used. Rules are transparent and testable.

### Exports (`services/subtitles.py`)
SRT and WebVTT, UTF-8 without a BOM, LF line endings. Milliseconds are rounded *before* splitting into fields,
which fixes the original `00:00:59,1000` bug. Cues are renumbered, blank lines inside cue text are removed
(they would terminate a cue), and `& < >` / `-->` are escaped in VTT. ASS was not added because no requirement
needs styled subtitles.

### Frontend (`frontend/`)
Next.js 15 App Router with a single client page, TypeScript, plain CSS (light/dark, responsive), and no UI
library.
- `lib/api.ts`: a typed client. Upload uses XHR to report upload progress, and errors surface the backend's
  `detail`/`code`.
- `lib/captions.ts`: pure helpers (time parse/format, validation that mirrors the backend, VTT for the
  preview), unit-tested with `node --test`.
- `CaptionEditor` keeps an in-browser media preview (object URL of the selected file) with a live VTT `<track>`
  built from the current edits. It highlights the active cue and lets you seek to any cue.
- The job id lives in `?job=`, so a reload resumes polling or the editor.

### Security notes
- Uploaded files are stored under server-generated UUID names with an allow-listed extension. The client's
  filename is used only for display and download names, after sanitisation. Job ids are validated as UUIDs
  before any path is built, so path traversal is not possible.
- Uploads are size-capped twice: by `Content-Length` before the body is spooled (middleware), and while copying
  for chunked bodies.
- CORS is restricted to configured origins without credentials.
- **No authentication.** This is intended for local or trusted use; see the README limitations.

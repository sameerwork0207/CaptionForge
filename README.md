# CaptionForge

CaptionForge automatically captions audio and video. You upload a file, it transcribes the speech locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), splits the text into readable timed captions,
and lets you review and edit them in the browser. You can then export **SRT** or **WebVTT** files.

- **Backend:** Python 3.11, FastAPI, FFmpeg, faster-whisper (CTranslate2), in `backend/`
- **Frontend:** Next.js 15, React 19, TypeScript, in `frontend/`
- Runs on one machine with no cloud services, database or API keys. The speech model is downloaded once from
  Hugging Face and then cached.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, [docs/TESTING.md](docs/TESTING.md) for tests, and
[docs/PROJECT_AUDIT.md](docs/PROJECT_AUDIT.md) for the original audit and its resolution.

---

## 1. Prerequisites

| Tool | Version used | Check |
|---|---|---|
| Python | 3.11 | `python --version` |
| FFmpeg + ffprobe | any recent (developed with 9.0) | `ffmpeg -version` and `ffprobe -version` |
| Node.js + npm | Node 20+ (developed with 24) | `node --version` |
| Disk | ~150 MB for the `base` model, ~300 MB for Python packages | |

**Installing FFmpeg**
- **Windows:** `winget install Gyan.FFmpeg`, then open a new terminal so `PATH` is updated.
- **macOS:** `brew install ffmpeg`
- **Debian/Ubuntu:** `sudo apt install ffmpeg`

FFmpeg must be on `PATH`. `GET /api/health` reports `"ffmpeg": true|false`. If it is missing, uploads are
rejected with HTTP 503 (`ffmpeg_unavailable`) and the UI shows a banner.

## 2. Backend setup

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt   # add -r requirements-dev.txt for tests
copy .env.example .env            # optional; every setting has a default
uvicorn app.main:app --port 8000
```

- API: http://localhost:8000. Interactive docs: http://localhost:8000/docs
- **Model download:** the first transcription downloads the selected model (`base`, about 142 MB) into the Hugging
  Face cache (`~/.cache/huggingface`, or `WHISPER_MODEL_DIR`). This needs internet access once and is then
  offline-capable. Download failures appear as a job error with code `model_unavailable`.
- Do not use `--reload` together with `--workers` > 1. The job queue lives inside a single process (see
  Architecture).

## 3. Frontend setup

```powershell
cd frontend
npm install
copy .env.example .env.local      # only if the backend is not at http://localhost:8000
npm run dev                       # development, http://localhost:3000
# or a production build:
npm run build
npm start
```

`NEXT_PUBLIC_API_BASE_URL` is baked in at build time, so rebuild after changing it. The frontend's origin must
be listed in the backend's `CORS_ORIGINS` (the default allows `http://localhost:3000`).

## 4. Using it

1. Open http://localhost:3000 and drop or select a file. Optionally choose the spoken language; the default is
   automatic detection.
2. The file is validated twice. The browser checks the extension and size. The server then checks the real
   content with ffprobe, the presence of an audio track, and the duration.
3. Watch the progress: queued → extracting audio → recognising speech (percentage of audio processed) → building
   captions.
4. Review the captions next to a preview of your media with the captions overlaid. You can edit text and times,
   insert or delete captions, and jump playback to any caption. Click **Save changes**.
5. Download **.srt** or **.vtt**. Unsaved edits are saved first.

Reloading the page keeps your job, because the job id is in the URL. The media preview is only available in the
tab that uploaded the file, because the server deletes uploads after processing.

## 5. Configuration

All settings are environment variables. They can also go in `backend/.env`. See
[`backend/.env.example`](backend/.env.example) for the full list.

| Variable | Default | Meaning |
|---|---|---|
| `WHISPER_MODEL` | `base` | `tiny` (fastest, least accurate), `base`, `small`, `medium`, `large-v3` (slowest, most accurate), or a local path |
| `WHISPER_DEVICE` | `cpu` | `cpu`, `cuda`, or `auto` (try CUDA, fall back to CPU) |
| `WHISPER_COMPUTE_TYPE` | `int8` on CPU | e.g. `float16` on GPU |
| `MAX_UPLOAD_MB` | 500 | upload size limit |
| `MAX_DURATION_SECONDS` | 10800 | media length limit (3 h) |
| `JOB_RETENTION_HOURS` | 24 | finished results are deleted after this; `0` keeps them forever |
| `MAX_QUEUED_JOBS` | 20 | pending + processing jobs accepted at once |
| `CAPTION_*` | 42 chars, 2 lines, 17 cps, 1–7 s, 0.6 s pause | segmentation rules |
| `CORS_ORIGINS` | localhost:3000 | allowed frontend origins |

**Choosing a model:** larger models are more accurate but slower and need more RAM. On the development
machine (i7-13700HX, CPU, `base`, int8), a 12 s clip took 1.3 s and a 2-minute clip took 29 s. Those clips were
synthetic speech; other hardware and real recordings will differ.

**GPU:** `WHISPER_DEVICE=cuda` requires the NVIDIA CUDA 12 runtime libraries (cuBLAS, cuDNN 9) that CTranslate2
uses. If they are missing, jobs fail with a clear `model_unavailable` message. `auto` silently uses the CPU
instead. GPU inference was **not** verified, because those libraries are absent on the development machine.

## 6. Supported input

- **Extensions:** `.wav .mp3 .m4a .aac .flac .ogg .opus` (audio) and `.mp4 .mov .mkv .webm` (video).
- The extension is only a first filter. The file must actually be decodable media with at least one audio
  stream. Only the **first** audio stream is transcribed.
- **Rejected with clear errors:** empty files (`empty_file`), non-media or corrupt files (`corrupt_media`),
  video without audio (`no_audio_stream`), oversize files (`file_too_large`, 413), media that is too long
  (`media_too_long`), and unsupported extensions (`unsupported_format`, 415).
- Silent audio, music without speech, and very short clips complete successfully with **zero captions**.

## 7. API

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /api/health` | liveness, FFmpeg + model state | |
| `GET /api/config` | formats, limits, languages | used by the UI |
| `POST /api/transcribe` | multipart `file` (+ optional `language`) | **202** `{job_id, status}`; 413/415/422/503 on errors |
| `GET /api/status/{id}` | `status`, `stage`, `progress` 0–1, `queue_position`, `error` | poll about every 1 s |
| `GET /api/result/{id}` | status + `segments`, `transcript`, `srt_content`, `language`, `duration` | result fields are null until `done` |
| `PUT /api/result/{id}/segments` | save edited captions `{segments:[{start,end,text}]}` | 409 before done; 422 on invalid times or overlaps |
| `GET /api/download/{id}?format=srt\|vtt` | subtitle file as an attachment | 409 before done |
| `DELETE /api/jobs/{id}` | delete a job, or cancel it if processing | 204 |

Job status values: `pending` → `processing` → `done` | `error`. All error responses have the shape
`{"detail": "<human message>", "code": "<machine code>"}`. Failed jobs carry `error: {code, message}`.

## 8. Storage and cleanup

- Uploads go to `backend/uploads/<job_id>/` and are **deleted as soon as the job finishes, fails or is
  cancelled**.
- Results go to `backend/outputs/<job_id>/` (`job.json`, `captions.srt`, `captions.vtt`). They are kept for
  `JOB_RETENTION_HOURS` after finishing, are swept at startup and periodically, and can be deleted at any time
  through `DELETE /api/jobs/{id}`.
- Finished jobs survive a server restart. Jobs that were queued or processing during a restart are marked
  `error` / `interrupted` and must be uploaded again.

## 9. Tests

```powershell
cd backend
pip install -r requirements-dev.txt
pytest -m "not model"     # fast: unit + API (mocked model), ~10 s
pytest                    # everything incl. real model + HTTP E2E (+ browser E2E if Playwright is installed)
cd ..\frontend; npm test; npm run typecheck
```

Details, including which tests are mocked and which use the real model, are in [docs/TESTING.md](docs/TESTING.md).

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| UI banner "Cannot reach the CaptionForge server" | Start the backend; check `NEXT_PUBLIC_API_BASE_URL` and rebuild the frontend. |
| Browser console shows a CORS error | Add the frontend URL to `CORS_ORIGINS` and restart the backend. |
| `ffmpeg_unavailable` / health shows `ffmpeg: false` | Install FFmpeg and open a new terminal so `PATH` is updated. |
| Job error `model_unavailable` | The first run needs internet access to download the model; check `WHISPER_MODEL`; on GPU use `WHISPER_DEVICE=cpu` or `auto`. |
| `DLL load failed ... Application Control policy has blocked this file` (Windows) | Windows Smart App Control blocked a newly downloaded native wheel. `requirements.txt` pins `av==12.3.0` for this reason; allow the file or use a version your policy accepts. |
| Progress stays at 0 % for a while | Recognition reports progress per decoded segment (about every 30 s of audio); the first job also loads the model. |
| Captions are wrong | Use a larger `WHISPER_MODEL` and/or set the language explicitly, then fix the remaining errors in the editor. |

## 11. Known limitations

- Transcription quality depends on the model and the audio. Accuracy was measured only on a synthetic English
  speech fixture (0 % WER with `base`), **not** on real-world recordings, accents, noise or music.
- Word timestamps are Whisper estimates, not forced alignment; expect occasional offsets of a few hundred
  milliseconds.
- There is no speaker diarization; captions do not mark speaker changes.
- Jobs run one at a time in a single server process. There is no horizontal scaling and no resume after a
  restart.
- There is no authentication. Anyone who can reach the API can upload files and read, edit or delete any job
  whose id they know. Run it locally or behind your own auth.
- Cancellation takes effect at the next decoded segment, not instantly; FFmpeg extraction itself cannot be
  interrupted.

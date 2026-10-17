# CaptionForge — Verification Plan

Prepared 2026-10-15, before any code change in this verification pass.

## 1. Starting state (verified)

| Item | Finding |
|---|---|
| Branch / remote | `main` tracking `origin/main` (`https://github.com/sameerwork0207/CaptionForge.git`); in sync after `git fetch` |
| Commits | 1 (`d0cee1b`) |
| Working tree | clean, no uncommitted changes |
| Docs vs code | README endpoint table, env vars, storage layout and status values match `app/api/routes.py`, `app/config.py`, `app/jobs.py` |
| Previous claim "144 backend tests pass" | **Reproduced**: `pytest` → 144 passed in 84 s. The 6 `ERROR` lines in `-rA` output are expected log records from negative tests, not test errors |
| Previous claim "6 frontend tests pass" | **Reproduced**: `npm test` → 6 pass; `npm run typecheck` clean |
| Frontend linting | **Not configured**: no ESLint in `package.json` |

## 2. Existing test inventory (85 test functions → 144 collected cases)

| File | Functions | Cases | Kind | Model | HTTP | Browser |
|---|---|---|---|---|---|---|
| test_captions.py | 20 | 23 | unit | – | – | – |
| test_subtitles.py | 8 | 16 | unit, hand-written parser | – | – | – |
| test_media.py | 9 | 21 | unit, real FFmpeg | – | – | – |
| test_config_utils.py | 8 | 24 | unit | real ModelManager error path | – | – |
| test_jobs.py | 9 | 10 | unit, stub pipeline | – | – | – |
| test_api.py | 23 | 36 | integration | **fake** (`FakeWhisper`) | TestClient (ASGI) | – |
| test_model_pipeline.py | 7 | 12 | integration | **real** | TestClient (1 test) | – |
| test_e2e_server.py | 1 | 1 | E2E | **real** | real uvicorn/TCP | – |
| test_e2e_browser.py | 1 | 1 | E2E | **real** | real uvicorn/TCP | Chrome (Playwright) |
| frontend lib/captions.test.ts | 6 | 6 | unit | – | – | – |

## 3. Gaps identified (to be closed in this pass)

**Component level**
- Segmentation: no randomised invariant (property) tests; no tests for NaN/negative/zero-duration/repeated timestamps from a model, very fast speech, or an over-long single word.
- Subtitles: validated only with an in-repo parser. An independent third-party parser (`srt`, `webvtt-py`) and the browser's own WebVTT parser should be added.
- Media: no tests for 8/48 kHz input, mismatched extension vs content, an undecodable codec, or a size exactly at the limit.
- Jobs: no tests for corrupt or malformed `job.json`, graceful shutdown while processing or with pending jobs, edit-after-delete races, or two jobs with the same filename.
- API: no tests for non-finite numbers in the edit body, wrong content type, malformed multipart, or duration boundaries.

**Connections**
- Cancellation → worker cleanup with the real model: covered once.
- Failure → frontend error display: only upload-time errors are covered. A *job-time* failure (passes probe, fails later) is not shown in the browser.
- Frontend API client ↔ backend schemas: only exercised implicitly.
- Saved captions → download: covered over HTTP. Download in the browser was only checked for SRT, and the in-browser WebVTT parse is not checked.

**Full stack**: scenarios B (WebVTT download in the UI), C with reload, E (silent video), F, G (UI cancel), I, J, K and L are missing or only partly covered in the browser.

**ML evaluation**: only synthetic speech was evaluated. Human speech with reference transcripts has not been evaluated.

## 4. Prerequisites and test data

| Prerequisite | Status |
|---|---|
| Python 3.11 venv `backend/.venv` with `requirements-e2e.txt` | present |
| FFmpeg 9.0.2 + ffprobe on PATH | present |
| Node 24 / npm 11, `frontend/node_modules` | present |
| Chrome 155 (Playwright channel `chrome`), Edge | present; **Firefox/Safari not installed** |
| `faster-whisper-base` in the HF cache | present |
| CUDA 12 runtime (cuBLAS/cuDNN) | **absent**, so GPU inference is BLOCKED |
| Human speech with reference text | LibriSpeech validation-clean sample (`hf-internal-testing/librispeech_asr_dummy`, CC BY 4.0): 73 utterances, 481 s, **one speaker** (1272). Downloaded at evaluation time, not committed |
| Multi-speaker / accented / non-English human speech with references | **not available**, so BLOCKED |
| Dependency audit tools | `npm audit` available; `pip-audit` to be installed in the venv for the audit only |

## 5. Execution commands

```powershell
cd backend
.venv\Scripts\python -m pytest -m "not model"                     # unit + mocked-model integration
.venv\Scripts\python -m pytest -m "model and not e2e"             # real model
.venv\Scripts\python -m pytest -m e2e                             # real server + browser
.venv\Scripts\python -m pytest                                    # all
.venv\Scripts\python -m eval.evaluate --librispeech <dir>         # ML evaluation (Phase 6)
.venv\Scripts\python -m pip_audit -r requirements-e2e.txt
cd ..\frontend
npm test; npm run typecheck; npm run lint; npm run build; npm audit
```

## 6. Acceptance criteria

1. Every module in `app/` has isolated tests for success and failure paths.
2. Each of the 17 module connections in the task brief is covered by at least one test using real implementations. The only mock allowed is the Whisper network, and each such mock must be labelled.
3. Generated SRT/VTT files are parsed by **independent** parsers: the `srt` and `webvtt-py` packages and Chrome's `TextTrack`.
4. Real-model runs on at least one genuine audio upload and one genuine video upload through the real server and real browser.
5. Every one of scenarios A–L is executed, through the browser where UI behaviour matters.
6. No job is left in `pending`/`processing` after any scenario; no files are left in `uploads/` after jobs finish.
7. Error responses never contain server paths, tracebacks or tool banners.
8. Real-model metrics are reported separately for synthetic and human speech: WER, CER, language, timing validity, RTF and peak memory.
9. Every defect found gets a reproduction, a fix and a regression test, followed by a full rerun.

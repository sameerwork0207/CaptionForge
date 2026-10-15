# CaptionForge — Testing

## Running

```powershell
cd backend
.venv\Scripts\activate
pip install -r requirements-dev.txt          # + requirements-e2e.txt for the browser test

pytest -m "not model"     # unit + API integration (mocked model)          ~8 s
pytest -m "model and not e2e"   # real faster-whisper pipeline tests        ~20 s
pytest -m e2e             # real uvicorn server (+ real Next.js build in Chrome/Edge)  ~35 s
pytest                    # everything                                      ~1 min

cd ..\frontend
npm test                  # pure TS helpers (node --test)
npm run typecheck
npm run build
```

Set `E2E_SCREENSHOT_DIR=<dir>` to save browser screenshots from the browser E2E test. The real-model tests use
`WHISPER_MODEL` / `WHISPER_DEVICE` from the environment (default `base` / `cpu`).

## What is real and what is mocked

| Suite | File | Speech model | Everything else |
|---|---|---|---|
| Segmentation unit | `test_captions.py` | n/a | pure logic |
| Export unit (+ strict SRT/VTT parsers) | `test_subtitles.py` | n/a | pure logic |
| Media unit | `test_media.py` | n/a | **real FFmpeg/ffprobe** (missing FFmpeg and timeouts simulated) |
| Config / filenames / model errors | `test_config_utils.py` | real `ModelManager` error path | |
| Job manager | `test_jobs.py` | n/a (**stub pipeline**) | real queue, threads, disk persistence, cleanup |
| API integration | `test_api.py` | **MOCKED** (`FakeWhisper`) | real HTTP stack (TestClient), FFmpeg, segmentation, export, jobs |
| Real ML pipeline | `test_model_pipeline.py` | **REAL faster-whisper** | real |
| HTTP E2E | `test_e2e_server.py` | **REAL** | real uvicorn process over TCP, including a restart |
| Browser E2E | `test_e2e_browser.py` | **REAL** | real uvicorn + production Next.js build + headless Chrome (Playwright) |
| Frontend helpers | `frontend/lib/captions.test.ts` | n/a | pure TS |

## Fixtures

- `backend/tests/fixtures/speech_en.wav` is 12.2 s of synthetic English speech (Windows SAPI voice, 16 kHz mono)
  with a known script (`SPEECH_SCRIPT` in `conftest.py`). It is committed so the tests are reproducible on any
  OS.
- Everything else is generated with FFmpeg at test time:
  - mp3, stereo 44.1 kHz AAC (m4a), H.264+AAC mp4, VP8+Opus webm, and video without an audio track
  - a 44 s clip (speech + 20 s silence + speech), 4 s of silence, a 3 s sine tone, and a 0.3 s clip
  - an empty file, random bytes named `.mp4`, a text file named `.wav`, and a truncated mp4

## Real-model acceptance criteria

Exact transcript equality is not required. Each real-model test checks:
- word error rate ≤ 20 % against the known script (normalised for case and punctuation)
- detected language `en`
- cues valid, monotonic, non-overlapping, and inside the media duration; first cue starts < 1 s
- long clip: no cue starts inside the 20 s silence, and the second speech block is captioned within 1.5 s of its
  start
- silence and pure tone produce **zero** cues; the 0.3 s clip completes without error

## Results (2026-10-15, Windows 11, i7-13700HX, Python 3.11.9, FFmpeg 9.0.2, model `base` CPU int8)

| Group | Result |
|---|---|
| `pytest -m "not model"` | **130 passed** |
| `pytest -m "model and not e2e"` | **12 passed** |
| `pytest -m e2e` (HTTP + browser) | **2 passed** |
| `pytest` (all) | **144 passed, 0 failed, 0 skipped** |
| `npm test` | **6 passed** |
| `npm run typecheck`, `npm run build`, `npm run dev` | **OK** |

Measured real-model behaviour on the fixtures:
- WER **0 %** on wav, mp3, stereo m4a and mp4 versions of the speech fixture. This is synthetic, clean speech
  and says nothing about real-world accuracy.
- Speed: model load 0.9–1.6 s (cached); 12.2 s clip in 1.3 s (RTF 0.10); 121.6 s clip in 29 s (RTF 0.24).

## Bugs found by testing and fixed

| Found by | Bug | Fix |
|---|---|---|
| original `test_pipeline.py` | faster-whisper 1.0.3 crashes on silent audio | upgraded to 1.1.1; regression tests `test_non_speech_and_tiny_clips_do_not_crash` |
| manual GPU probe | a second CUDA initialisation after a failure hangs CTranslate2, which would have blocked the worker forever | CUDA failure is sticky per process; regression test `test_repeated_cuda_attempts_never_hang` |
| `test_subtitles` | `59.9996 s` → `00:00:59,1000` | round to ms before splitting |
| `test_captions` | lines > 42 chars, early cue starts across pauses, overlaps | segmentation rewrite |
| environment | `av` 18.1 DLL blocked by Windows Application Control | pinned `av==12.3.0` |

## Not covered / not verified

- **GPU (CUDA) inference: BLOCKED.** cuBLAS/cuDNN are not installed on the test machine. Only the error path and
  the `auto` → CPU fallback are verified.
- **Real-world recordings: NOT RUN.** There is no licensed human-speech corpus in the repository; accuracy on
  noisy, accented or multi-speaker audio is unmeasured.
- **Very large files** (hundreds of MB, hours long): NOT RUN. Limits are enforced by the code paths tested with
  small limits.
- **Non-Chromium browsers** (Firefox, Safari): NOT RUN.
- **Linux/macOS:** NOT RUN. The code avoids Windows-specific paths, but only Windows was exercised.

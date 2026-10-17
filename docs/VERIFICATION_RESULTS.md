# CaptionForge — Verification Results

Verification pass of 2026-10-15, starting from commit `d0cee1b`. Every result below was **executed**. Statuses:
PASS, FAIL, BLOCKED (missing prerequisite), NOT RUN, SKIPPED (deliberate, with reason).

## 0. Independent reproduction pass (2026-10-17)

A later pass re-ran the suites below from the working tree exactly as documented, without changing model
behaviour, to confirm the 2026-10-15 figures rather than take them on trust.

| Suite | Command | Result |
|---|---|---|
| Backend unit + mocked-model | `pytest -m "not model"` | **PASS**: 569 passed, 43 deselected (matches) |
| Backend real-model | `pytest -m "model and not e2e"` | **PASS**: 12 passed (matches) |
| Real HTTP E2E | `pytest tests/test_e2e_server.py` | **PASS**: 1 passed (matches) |
| Real browser E2E | `pytest tests/test_e2e_browser.py` | **PASS**: 1 passed (matches) |
| Scenarios A–L | `pytest tests/test_e2e_scenarios.py` | **PASS**: 27 passed, 2 skipped (LibriSpeech not set) — matches |
| Frontend unit | `npm test` | **PASS**: 8 passed (matches) |
| Frontend typecheck / lint / build | `npm run typecheck/lint/build` | **PASS** (matches) |
| `npm audit` | `npm audit` | At time of this pass: matched §8 (`next`/`postcss`, `braces`). **Since resolved** (same day, later in this engagement): `next`/`postcss` fixed via `overrides: postcss@8.5.30`, no Next 16 needed; `braces` remains unresolved (no upstream fix exists) |
| `pip-audit` | `pip_audit -r requirements-e2e.txt` | At time of this pass: starlette 0.41.3 and pytest 8.3.4 flagged, matching §8. **starlette since resolved** (same engagement, `fastapi` → `0.135.0`, `starlette` → `1.8.0`, 0 findings); `pytest 8.3.4` remains flagged (UNIX-only, not applicable on Windows), untouched by design |

**Documentation discrepancy found and fixed:** README §11 still claimed accuracy was "measured only on a
synthetic English speech fixture," which had gone stale once this document's human-speech evaluation (§7) was
added. Rather than trust either document, the LibriSpeech dummy dataset (`hf-internal-testing/librispeech_asr_dummy`,
73 utterances, speaker 1272) was freshly downloaded from Hugging Face and the committed, unmodified harness
(`eval/evaluate.py --model base --conditions clean`) was rerun on all 73 utterances: **WER 9.04 %, CER 4.25 %**,
exactly matching §7. README was corrected to state the human-speech figure instead of the stale synthetic-only
claim. The noise/speed conditions, the `long` run, and the `tiny`/`small` models were **not** re-executed in this
pass (see §9); treat those specific figures as carried over from 2026-10-15, not re-verified today.

A side effect was found and corrected during this pass: a plain `npm run build` (outside the E2E harness that
normally restores it, per D14) rewrote `frontend/tsconfig.json`'s `include` list. It was restored to its prior
state; this is a Next.js build side effect, not a product defect, and is already covered by D14's test.

No new product defects were found in this pass.

## 1. Environment

| Item | Value |
|---|---|
| OS / CPU / RAM | Windows 11 Home 10.0.26200, Intel i7-13700HX (24 logical CPUs), 15.7 GB RAM (about 2–3 GB free during the run) |
| GPU | RTX 4050 Laptop 6 GB. **CUDA 12 runtime libraries absent** (`cublas64_12.dll` not found) |
| Python / FFmpeg | 3.11.9 (venv `backend/.venv`) / FFmpeg 9.0.2 |
| Node / npm / browser | Node 24.12.0, npm 11.6.2, Chrome 155.0.8059.40 via Playwright 1.49.1. Firefox and Safari not installed |
| Model | `faster-whisper` 1.1.1, `base`, CPU, int8 |

## 2. Baseline reproduction (before any change)

| Claim in previous report | Command | Result |
|---|---|---|
| 144 backend tests pass | `.venv\Scripts\python -m pytest` | **PASS**: 144 passed (84 s). The 6 `ERROR` lines in `-rA` output are expected log records from negative tests |
| 6 frontend tests pass | `npm test` | **PASS**: 6/6 |
| typecheck | `npm run typecheck` | **PASS** |
| lint | n/a | **Not configured** (fixed in this pass) |

## 3. Final results (after fixes)

Test **cases** are collected pytest items; parametrised functions count once per parameter set. Function and
assertion counts are shown separately.

| Suite | Command (from `backend/`, or `frontend/` for npm) | Functions | Cases | Result |
|---|---|---|---|---|
| Backend non-E2E (unit, property, contract, mocked-model API, real-model) | `.venv\Scripts\python -m pytest -m "not e2e"` | 121 | 581 | **581 passed, 0 failed, 0 skipped** |
| of which mocked-model and pure-logic | `pytest -m "not model"` | 114 | 569 | **569 passed** |
| of which real model | `pytest -m "model and not e2e"` | 7 | 12 | **12 passed** |
| Real-server HTTP E2E | `pytest tests/test_e2e_server.py` | 1 | 1 | **1 passed** |
| Browser E2E (original journey) | `pytest tests/test_e2e_browser.py` | 1 | 1 | **1 passed** |
| Full-stack scenarios A–L, real model + Chrome | `LIBRISPEECH_DIR=<dir> pytest tests/test_e2e_scenarios.py` | 18 | 29 | **29 passed** (200 s) |
| **Backend total** | | **141** | **612** | **612 passed** |
| Frontend unit | `npm test` | 8 | 8 | **8 passed** |
| Frontend typecheck / lint / build | `npm run typecheck`, `npm run lint`, `npm run build` | | | **PASS / PASS (0 problems) / PASS** |
| Clean backend env | new venv + `pip install -r requirements-dev.txt` + `pytest -m "not model"` and real-model files | | 569 + 13 | **569 passed, 2 skipped** (browser modules: Playwright is only in `requirements-e2e.txt`, by design), **13 passed** |
| Clean frontend env | copy of `frontend/` + `npm ci` + test/typecheck/lint/build | | | **PASS** (after fixing D13) |

The property suite alone executes 300 randomised segmentation cases with five invariants each, and 40 randomised
export round-trips through two third-party parsers.

## 4. Defects found and fixed in this pass

Every defect was reproduced first (command output or failing test), then fixed and given a regression test, and
the relevant suites were rerun.

| # | Found by | Defect | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| D1 | manual probe | `PUT .../segments` with `Infinity` → **HTTP 500** | Python's JSON parser accepts `Infinity`/`NaN`; `round(inf)` crashed export | `allow_inf_nan=False`, upper bound 359 999 s | `test_edit_rejects_invalid_bodies` (8 bodies) |
| D2 | manual probe | One malformed `job.json` → **server could not start** (`KeyError: 'status'`) | recovery trusted persisted data | validate records (id = dir name, status, segments, error); skip bad ones | `test_malformed_job_records_do_not_break_startup` |
| D3 | manual probe | Graceful shutdown **deleted** the in-flight job and all pending jobs (404 after restart; docs promised `interrupted`) and ran FFmpeg for each pending job | shutdown reused the user-cancel path | `_stopping` flag: pending jobs left on disk, in-flight job recorded `interrupted` | `test_graceful_shutdown_marks_jobs_interrupted` |
| D4 | review | Edit racing a delete → `KeyError` → 500 | missing-job case not mapped | `JobNotFoundError` (404) | `test_edit_after_delete_is_404_not_500` |
| D5 | review + probe | Audio with no decoder accepted (202), failed later | ffprobe reports no `codec_name` | reject at upload (`unsupported_codec`, 422) | `test_undecodable_codec_rejected_at_probe`, `test_api_rejects_unknown_codec_at_upload`, scenario D |
| D6 | independent parser | Caption text containing `-->` written verbatim into SRT; a line can be misread as a timing line | SRT has no escaping | write `->` in SRT (VTT already escaped) | `test_tricky_text_roundtrips…`, `test_random_pipeline_output_is_valid` |
| D7 | independent parser | Times ≥ 100 h produced `100:00:00,999`, which strict parsers reject | hour field unbounded | clamp to 99:59:59.999; `MAX_DURATION_SECONDS` ≤ 99 h | `test_timestamps_beyond_99_hours_are_clamped`, config tests |
| D8 | hostile-filename test | Filenames > 200 chars: extension truncated → valid file rejected **415** | truncation before extension check | shorten the stem, keep the extension | `test_long_filename_keeps_extension`, `test_hostile_filenames` |
| D9 | browser scenario (failed pre-fix) | "Retrying…" banner stayed after the connection recovered | error state never cleared on a successful poll | clear on success | `test_status_poll_blip_recovers_and_clears_message` |
| D10 | browser scenario (failed pre-fix) | A failed download **navigated the tab to raw JSON**, leaving the app | `window.location.href = url` | fetch → blob → save; error shown in page | `test_download_failure_is_shown_not_navigated` |
| D11 | scenario L design | 10 s limit reported as "the maximum is **0 minutes**"; UI showed "up to 0 min" | durations always formatted in whole minutes | `format_duration` / `formatDuration` | `test_duration_messages_are_readable`, `formatDuration` unit test, scenario L |
| D12 | browser console check (failed pre-fix) | Every page load logged a 404 (`/favicon.ico`) | no app icon | `app/icon.svg` | console assertions in scenarios A, B, C, E, G |
| D13 | clean `npm ci` | **`npm ci` failed** (lockfile out of sync after adding ESLint) | platform-specific optional deps missing from lock | regenerated `package-lock.json`; clean `npm ci` now PASS | clean-env reproduction (§3) |
| D14 | `git status` | E2E builds rewrote the tracked `tsconfig.json` (custom distDir added) | `next build` side effect | E2E harness restores `tsconfig.json`; file cleaned | `git status` clean after E2E run |
| – | property tests | Long Latin word **split mid-word** across lines | CJK char-split fallback applied to all scripts | char-split only for no-space scripts | `test_invariants[*]` (I3), `test_long_unbroken_word_is_kept_whole` |
| – | property tests | NaN/∞ model timestamps propagated into cues (would crash export) | no sanitising of model timings | clamp non-finite/negative/backwards times | `test_invariants[*-hostile]`, `test_odd_model_output_still_yields_valid_files` |
| – | consistency | Backend rounded half-ms to even, frontend half-up | `round()` vs `Math.round` | both half-up | `test_boundary_timestamps` |
| – | UX gap | No way to delete a finished job from the UI (API existed) | missing control | "Delete from server" button with confirmation | `test_G_delete_completed_job` |
| – | a11y (failed pre-fix) | Progress bars had no `progressbar` role/values | markup | ARIA `role="progressbar"` + values | `test_keyboard_and_accessibility` |

**Test-side corrections (not product defects):** a float-equality comparison in a new test; the F-scenario model
copy needed faster-whisper's own file patterns; the L-scenario size files were also over the duration limit;
an old test asserted the pre-D6 SRT behaviour.

**Pre-fix evidence:** the first scenario run on unfixed code gave `6 failed, 17 passed, 6 skipped`. The failures
were D9, D10, D12, the missing delete control, the missing ARIA roles, and one test-side issue (F). After the
fixes: `29 passed`.

## 5. Module connections (integration coverage)

"Real" means the real implementation runs. Where the model is MOCKED, `FakeWhisper` replaces **only**
`WhisperModel.transcribe`; upload handling, ffprobe/FFmpeg, segmentation, export, the job queue, persistence and
HTTP stay real.

| # | Connection | Covered by | Model |
|---|---|---|---|
| 1 | upload → validation | `test_api::test_rejected_uploads`, `test_components_extra::test_hostile_filenames`, `test_upload_size_boundary`, scenario D/L | mocked / real |
| 2 | validation → ffprobe | `test_media::test_probe_*`, `test_api_rejects_unknown_codec_at_upload` | n/a |
| 3 | FFmpeg → extraction | `test_extract_audio_to_16k_mono`, `test_more_formats_extract_to_16k_mono` (12 formats/rates) | n/a |
| 4 | extracted audio → transcription | `test_model_pipeline::*` (real), scenarios A/B | **real** |
| 5 | model output → timestamp normalisation | `test_odd_model_output_still_yields_valid_files`, property `hostile` cases | mocked |
| 6 | normalisation → segmentation | property tests (300), `test_captions` | n/a |
| 7 | segmentation → caption validation | `normalize_edited_captions` tests, `test_edit_validation`, scenario C | n/a |
| 8 | captions → SRT/VTT | `test_exports_independent` (`srt`, `webvtt-py`), Chrome `TextTrack` in scenario B | n/a |
| 9 | pipeline → job state | `test_jobs::*`, `test_e2e_server` (state sequence) | stub / **real** |
| 10 | pipeline → persisted results | `test_concurrent_edits_are_atomic` (job.json = SRT = VTT), restart tests | mocked / **real** |
| 11 | API → job manager | `test_api::*`, `test_edit_after_delete_is_404_not_500` | mocked |
| 12 | API → export files | `test_missing_output_file`, `test_same_filename_jobs_are_independent` | mocked |
| 13 | frontend client ↔ backend schemas | `test_frontend_contract` (OpenAPI vs `lib/api.ts`, mutation-checked) + browser network assertions | n/a / **real** |
| 14 | editor → edit API → saved | scenario C (UI edit → PUT 200 → reload → other context) | **real** |
| 15 | saved captions → download | scenarios C, E (download after edit, auto-save before download) | **real** |
| 16 | cancellation → worker cleanup | `test_cancel_real_job_cleans_up`, scenario G | **real** |
| 17 | failure → error status → UI | scenarios D, F, H (UI shows server message and code) | **real** |

## 6. Full-stack scenarios (real backend, real model, production frontend, Chrome)

| Scenario | What was verified | Status |
|---|---|---|
| A audio | mp3 → progress → 4 cues → **SRT downloaded and parsed by `srt` lib**; browser traffic: POST 202, status 200×n, result 200, download 200; no console errors | PASS |
| A2 human audio | 12 LibriSpeech utterances (≈90 s) as FLAC → SRT: 31 cues, **WER 8.9 %** | PASS |
| B video | webm (VP8+Opus) → `<video>` preview → **VTT parsed by `webvtt-py` and by Chrome's TextTrack** (cue times/texts identical) | PASS |
| B2 human video | same LibriSpeech audio muxed into H.264 mp4 → VTT: 30 cues, **WER 10.2 %** | PASS |
| C editing | edit Unicode/HTML-ish text and times; overlap rejected client-side; insert cue; save (PUT 200); reload; auto-save before download | PASS |
| D invalid | .txt and empty (client-side, no request sent); random bytes `.mp4`, text `.wav`, unknown codec (server 422, readable message, no paths); no stuck jobs, no leftover uploads | PASS (5 cases) |
| E silent | silent WAV and video with a silent audio track → "No speech was detected", 0 cues, valid empty SRT; manual caption added and saved | PASS (2 cases) |
| F failure | model directory missing → job `error/model_unavailable` shown in UI → model installed → **same server process** completes the next job | PASS |
| G cancel / delete | cancel during recognition (DELETE 204, status 404, upload removed); delete a finished job from the UI; reopening it explains it no longer exists | PASS (2 cases) |
| H restart | backend **hard-killed** mid-job: UI shows "Retrying…", then `interrupted` after restart; finished job still loads; new jobs run | PASS |
| I concurrent | 3 tabs upload at once: queue position shown, 3 distinct ids, each transcript matches its own file, FIFO order | PASS |
| J repeated | same file 3× with a failed upload in between: identical captions, distinct ids, no leftover files | PASS |
| K reload | reload and a fresh browser context (no shared storage) show the saved edits | PASS |
| L limits | 1 MiB limit: −1 B and exact accepted, +1 B rejected client-side **and** by the API (413); 10 s limit: 9.5 s and 10.0 s accepted, 10.5 s rejected (422, "maximum is 10 seconds") | PASS (6 cases) |
| Network blip | status requests aborted → "Retrying…" → restored → message clears, processing continues | PASS |
| Download failure | download 404 → error shown in page, URL unchanged, editor intact | PASS |
| Keyboard / a11y | Tab to dropzone, Enter opens file chooser, Enter submits; `progressbar` role present; every input/button has an accessible name | PASS |
| Mobile 360 px | upload and editor pages have no horizontal overflow | PASS |
| Long transcript | 121.6 s file → ≥ 30 cues rendered, SRT cue count equals editor rows | PASS |
| Firefox / Safari | browsers not installed | **BLOCKED** |

## 7. Real-model evaluation (Phase 6)

Harness: `backend/eval/evaluate.py`. It runs the real pipeline per utterance. WER/CER normalisation:
lower-case, keep letters, digits and apostrophes, collapse whitespace, no number normalisation.

**Data**
- *Human:* LibriSpeech dev-clean sample (`hf-internal-testing/librispeech_asr_dummy`, CC BY 4.0), 73 utterances,
  481 s, **one male speaker (1272), read English**. Not committed. It was downloaded into a scratch folder and
  extracted from parquet with a separate script.
- *Synthetic:* the committed TTS fixture.

**Independent re-verification (2026-10-17):** the README previously stated accuracy was measured only on the
synthetic fixture, which no longer matched this section. To resolve the discrepancy rather than trust either
document, the same public dataset was re-downloaded fresh (not from any cached/committed copy) and the
committed harness (`eval/evaluate.py`) was rerun unmodified on the `clean` condition, all 73 utterances:
**WER 9.04 %, CER 4.25 %**, both matching this table exactly. RTF measured 0.152 on the re-run machine state vs.
0.261 below; RTF is sensitive to machine load and is not treated as a correctness figure. The noise/speed
conditions and the `long`/`tiny`/`small` runs were not re-executed in this pass (see §9); they are carried over
from the original run and are unverified in this pass. README §11 has been corrected to cite the human-speech
figure instead of claiming synthetic-only measurement.

**Results, `base`, CPU int8** (from the run log). Noise is white noise mixed at the given SNR relative to the
clip's mean level; speed uses `atempo`.

| Data / condition | Utterances | WER | CER | RTF |
|---|---|---|---|---|
| Synthetic TTS, clean (test suite) | 1 (and its mp3/m4a/mp4 variants) | 0.0 % | – | ≈0.10 |
| **Human, clean** | 73 | **9.04 %** | 4.25 % | 0.261 |
| Human, +noise SNR 10 dB | 73 | 23.13 % | 12.46 % | 0.335 |
| Human, +noise SNR 0 dB | 73 | 68.70 % | 42.79 % | 0.399 |
| Human, 0.8× speed (slow) | 73 | 9.74 % | 4.81 % | 0.281 |
| Human, 1.25× speed (fast) | 73 | 11.83 % | 5.70 % | 0.382 |
| Human via browser E2E (12 utt., FLAC) | 12 | 8.9 % | – | – |
| Human via browser E2E (12 utt., mp4) | 12 | 10.2 % | – | – |

RTF here is per utterance, including FFmpeg and the per-file model call for 5–15 s clips. It is higher than for
long files: a 121.6 s file measured RTF 0.24 earlier. Other observations from the test suites:
- language detection returned `en` for every real-model test;
- every cue in all real-model runs was valid, ordered and non-overlapping (asserted);
- silence and a pure tone produced 0 cues;
- no cue started inside the 20 s silence of the long clip;
- the `base` model server process used about 270–280 MB working set (Windows task list, two concurrent E2E
  backends).

**Not completed (NOT RUN):** the evaluation process was stopped by Claude Code because the machine ran critically
low on memory while it ran in the background. The machine was running other memory-heavy applications, with
about 2 GB free. These did not run:
- `base` long-file boundary test and peak-memory figure;
- `tiny` and `small` models.

They were not restarted automatically. To reproduce:
`python -m eval.evaluate --model base --librispeech <dir> --long` (and `--model tiny|small`).

**What these numbers do not show:** accents, spontaneous or conversational speech, overlapping speakers, music
beds, non-English languages, or real recording noise. No suitable referenced data was available, so these are
**BLOCKED**. Speaker diarization is not implemented.

## 8. Reliability and security checks

| Check | Method | Result |
|---|---|---|
| Path traversal in filenames | `../../evil.wav`, `..\..\`, `C:\fakepath\`, NUL byte, `<script>`, 400-char names, through the API | PASS: nothing written outside configured dirs; names sanitised (D8 fixed) |
| Path traversal / invalid job ids | `..%2F..`, non-UUIDs, unknown UUIDs on all 4 id routes | PASS: 404 |
| Malformed multipart / wrong content type | JSON body, text field, garbage boundary, missing boundary | PASS: 422, no leak |
| Oversized input | Content-Length pre-check and chunked copy cap; boundary ±1 byte | PASS |
| Non-finite / huge numbers in JSON | `Infinity`, `NaN`, `1e308` | PASS after D1 |
| Concurrent edits | 12 parallel PUTs | PASS: record, SRT and VTT identical (atomic) |
| File collisions | same filename ×3, concurrent uploads | PASS: independent dirs per UUID |
| Temp / leftover files | `uploads/` empty after success, failure, rejection, cancel, restart | PASS |
| Subprocess timeout / failure | simulated `TimeoutExpired` and non-zero exit | PASS: `media_timeout` / `decode_failed`, no stderr leak |
| Tracebacks or paths in responses | `assert_no_leak` on error bodies; UI text checked in scenario D | PASS |
| CORS | allowed origin echoed; unknown origin gets no ACAO; `Content-Disposition` exposed | PASS |
| Unauthenticated access | by design: any client can use any job id it knows | **Documented limitation** (KNOWN_LIMITATIONS) |
| Logs | contain filenames and server paths (server-side only); no secrets exist in the app | Documented |
| `pip-audit` 2.10.1 on `backend/.venv` | before: 18 advisories in pytest, python-multipart (6), setuptools (4), starlette (7) | **python-multipart upgraded 0.0.20 → 0.0.31** (fixes its 6 advisories). **starlette's 7 advisories since resolved** via `fastapi` 0.115.6 → 0.135.0 (resolves `starlette` → 1.8.0); `pip-audit` confirms 0 remaining starlette findings; full suite rerun 610 passed/2 skipped/0 failed. Remaining: setuptools 65.5.0 (venv bootstrap copy, not used at runtime), pytest 8.3.4 (UNIX `/tmp` issue, not applicable on Windows, deferred) |
| npm audit | Production: Next.js/PostCSS advisories resolved in the current dependency tree via the npm override `"postcss": "8.5.30"`; confirmed by `npm audit`, `npm ls postcss --all`, and successful build/dev/typecheck/lint/unit/E2E verification. No Next.js 16 upgrade required for this fix. Dev-only: the braces chain in eslint-config-next remains; no upstream fix is available. | Resolved / documented remaining risk |

## 9. Blockers and items not verified

| Item | Status | Reason |
|---|---|---|
| GPU (CUDA) inference | BLOCKED | cuBLAS/cuDNN not installed (no machine changes made). CPU fallback and the error path are verified |
| Firefox / Safari | BLOCKED | not installed |
| Linux / macOS | NOT RUN | only Windows available |
| `tiny` / `small` model evaluation, long-file boundary metric, peak memory | NOT RUN | stopped by Claude Code because the machine was low on memory; awaiting a decision to rerun |
| Accented / conversational / multi-speaker / non-English human speech with references | BLOCKED | no lawful referenced data available in this environment |
| Speaker diarization | not implemented | out of scope; not claimed |

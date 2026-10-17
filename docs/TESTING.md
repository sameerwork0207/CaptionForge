# CaptionForge — Testing

The latest executed results and the defect log are in [VERIFICATION_RESULTS.md](VERIFICATION_RESULTS.md). The
plan and acceptance criteria are in [VERIFICATION_PLAN.md](VERIFICATION_PLAN.md).

## Running

```powershell
cd backend
.venv\Scripts\activate
pip install -r requirements-dev.txt      # + requirements-e2e.txt (Playwright) for browser tests

pytest -m "not model"           # unit, property, contract, mocked-model API      569 cases, ~20 s
pytest -m "model and not e2e"   # real faster-whisper pipeline                      12 cases, ~25 s
pytest -m e2e                   # real uvicorn + production Next.js + Chrome         31 cases, ~4 min
pytest                          # everything                                       612 cases

# optional: genuine human-speech browser workflows (A2/B2) need LibriSpeech files + manifest.json
$env:LIBRISPEECH_DIR = "C:\path\to\librispeech_sample"; pytest tests/test_e2e_scenarios.py

# real-model accuracy evaluation (WER/CER, noise, speed, RTF, peak memory)
python -m eval.evaluate --model base --librispeech $env:LIBRISPEECH_DIR --long

cd ..\frontend
npm ci; npm test; npm run typecheck; npm run lint; npm run build; npm audit
```

Set `E2E_SCREENSHOT_DIR=<dir>` to keep browser screenshots. The browser tests need Chrome or Edge, which
Playwright drives through its `chrome`/`msedge` channel, so no browser download is needed.

## Inventory: what is real and what is mocked

| File | Cases | Kind | Speech model | Notes |
|---|---|---|---|---|
| test_captions.py | 23 | unit | – | segmentation rules, edit validation |
| test_segmentation_properties.py | 304 | property (seeded random) | – | 300 random streams incl. NaN/negative/backwards timings; 5 invariants |
| test_subtitles.py | 15 | unit | – | timestamps, escaping, in-repo strict parser |
| test_exports_independent.py | 66 | unit | – | **third-party `srt` and `webvtt-py` parsers** |
| test_media.py | 28 | unit | – | **real FFmpeg/ffprobe**; missing FFmpeg / timeout / exit codes simulated |
| test_config_utils.py | 23 | unit | real `ModelManager` error path | config validation, filename sanitising |
| test_jobs.py | 10 | unit | stub pipeline | queue, FIFO, cancel, retention, recovery |
| test_api.py | 38 | integration | **MOCKED** (`FakeWhisper`) | real HTTP stack, FFmpeg, segmentation, export, jobs |
| test_components_extra.py | 55 | integration | **MOCKED** where a model is needed | 12 formats/rates, size and duration boundaries, malformed requests, hostile filenames, odd model output, concurrent edits, corrupt records, graceful shutdown, CORS |
| test_frontend_contract.py | 7 | contract | – | `frontend/lib/api.ts` interfaces vs backend OpenAPI and live responses |
| test_model_pipeline.py | 12 | integration | **REAL** | accuracy on fixtures, silence, long clip, cancel, CUDA retry |
| test_e2e_server.py | 1 | E2E | **REAL** | real uvicorn over TCP, including restart |
| test_e2e_browser.py | 1 | E2E | **REAL** | original browser journey |
| test_e2e_scenarios.py | 29 | E2E | **REAL** | scenarios A–L, network/console assertions, a11y, mobile, resilience |
| frontend `lib/captions.test.ts` | 8 | unit | – | time parse/format, validation, VTT, duration and filename helpers |

`FakeWhisper` (in `tests/conftest.py`) replaces only `WhisperModel.transcribe`. Everything around it runs for
real.

## Fixtures

- `tests/fixtures/speech_en.wav`: 12.2 s of synthetic English speech (Windows SAPI) with a known script.
- Everything else is generated with FFmpeg at test time:
  - formats: mp3, m4a, aac, flac, ogg, opus, mp4, webm, mkv and mov;
  - sample rates and channels: 8 kHz, 48 kHz stereo;
  - video variants: video without audio, video with a silent audio track;
  - edge cases: silence, a pure tone, a 0.3 s clip, 44 s and 2 min clips, exact-size and exact-duration
    boundary files, an empty file, random bytes, a text file named `.wav`, a truncated mp4, and a WAV with an
    unknown codec tag.
- Human speech (optional, not committed): LibriSpeech dev-clean sample (CC BY 4.0), as described in
  VERIFICATION_RESULTS §7.

## Real-model acceptance criteria

- WER ≤ 20 % on the synthetic fixture.
- WER ≤ 15 % for the human-speech browser workflows.
- Language detected as `en`.
- Cues are valid, ordered and non-overlapping, and lie inside the media duration.
- No cue inside long silences.
- Silence and a pure tone produce zero cues.
- Very short clips complete without error.

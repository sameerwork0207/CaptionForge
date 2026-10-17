# CaptionForge — Known Limitations

As of the verification pass on 2026-10-15. "Verified" means it was executed; see
[VERIFICATION_RESULTS.md](VERIFICATION_RESULTS.md) for evidence.

## Functional scope

| Area | Limitation |
|---|---|
| Speaker diarization | **Not implemented.** Captions never mark speaker changes. Multi-speaker audio is transcribed as one stream. |
| Subtitle formats | SRT and WebVTT only. No ASS/SSA styling, positioning or burn-in. |
| Audio streams | Only the **first** audio stream of a file is transcribed. |
| Translation | Not supported. Whisper's `translate` task is not exposed. |
| Media preview | Only in the browser tab that uploaded the file. The server deletes uploads after processing, so a reopened job has no player. |
| Edited text | User edits are saved as typed. Line-length and reading-speed rules apply only to *generated* captions. |
| Delete/cancel | Cancellation takes effect at the next decoded segment (Whisper decodes 30 s windows), so it can take several seconds. FFmpeg extraction cannot be interrupted. |

## Accuracy (measured, not guaranteed)

- Measured on **one** human speaker (LibriSpeech speaker 1272, read English) and synthetic TTS. Accents, spontaneous
  speech, overlapping speakers, music and non-English languages were **not** evaluated with reference
  transcripts.
- WER rises sharply in noise. The figures are in VERIFICATION_RESULTS §7. Choose a larger model for noisy audio.
- Word timestamps are Whisper's cross-attention estimates, not forced alignment. Cue boundaries can be off by a few
  hundred ms.
- Whisper can hallucinate text on non-speech audio. VAD and a punctuation-only filter reduce this; on the test
  fixtures, silence and a pure tone produced zero captions. This is not a guarantee for every kind of noise or
  music.
- WER normalisation does not convert numbers ("2" vs "two" counts as an error).

## Segmentation rules that are best-effort

The rules in `SegmentationConfig` hold for all generated cues **except** in these cases, where the timing data makes
them impossible:
- A single word longer than `max_chars_per_line` (URLs, long compounds) is kept whole on one line rather than broken
  mid-word.
- Reading speed (`max_cps`) is met by extending the cue's end time, but never into the next cue. Very fast speech
  therefore exceeds 17 cps. The measured violation rate is in the results doc.
- A single word that lasts longer than `max_duration` produces a cue longer than `max_duration`.
- Times are clamped to 99:59:59.999 (the largest two-digit-hour SRT/VTT timestamp). Media longer than 99 h is
  rejected by configuration validation.

## Operations

| Area | Limitation |
|---|---|
| Concurrency | One job at a time in one process; others queue (default limit 20). Running `uvicorn --workers N` is unsupported because each worker would have its own queue and model. |
| Restart | Finished jobs persist. Jobs that are pending or processing at shutdown or crash become `error/interrupted` and must be re-uploaded. There is no resume. |
| Storage | Results are kept `JOB_RETENTION_HOURS` (default 24). Uploads are deleted after processing. There is no disk-space pre-check, so a full disk fails the job. |
| GPU | `WHISPER_DEVICE=cuda` needs the CUDA 12 cuBLAS/cuDNN 9 libraries. **GPU inference is not verified**, because those libraries are absent on the test machine. A failed CUDA initialisation is remembered for the life of the process, so restart after installing them. |
| Platforms | Verified on Windows 11 only. Linux/macOS were not run. |
| Browsers | Verified in Chrome 155 (Chromium). Firefox and Safari are **not installed** on the test machine and were not run. |

## Security model (local / trusted-network use)

- **No authentication or authorisation.** Anyone who can reach the API can upload files, and anyone who knows a job
  id (a random UUIDv4) can read, edit, download or delete that job. Do not expose the server to untrusted networks
  without putting authentication in front of it.
- CORS only restricts *browsers* on other origins; it is not access control.
- Server logs contain original filenames and server-side paths (for debugging). Clients never receive paths or
  tracebacks.
- Known dependency advisories (see results §8):
  - ~~`starlette 0.41.3`~~ **Resolved 2026-10-17:** `fastapi` bumped to `0.135.0`, resolving `starlette` to `1.8.0`;
    `pip-audit` confirms the 7 prior starlette advisories (including the `Range`-header DoS) are gone. Full backend
    suite rerun clean (610 passed, 2 skipped, 0 failed).
  - ~~Next.js' bundled `postcss`~~ **Resolved 2026-10-17**, without a Next 16 upgrade: an npm `overrides` pin forces
    `postcss` to `8.5.30` underneath the existing `next@15.5.28`. `npm audit` confirms the 4 prior postcss advisories
    are gone. Build, dev server, typecheck, lint, unit tests, and both browser E2E suites rerun clean.
  - Dev-only lint tooling: `braces` DoS.

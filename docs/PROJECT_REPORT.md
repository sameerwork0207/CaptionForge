# CaptionForge: Project Report (Deep Learning Perspective)

## 1. What the project does
CaptionForge turns any audio or video file into timed subtitles (SRT / WebVTT). A user uploads a file in a web
page, a speech-recognition neural network transcribes it, a rule-based algorithm cuts the text into readable
captions, and the user can review and edit them next to a media preview before downloading.

Everything runs locally: no cloud API, no database, no API key.

## 2. Deep learning technology used

### 2.1 Model: OpenAI Whisper (via faster-whisper)
- **Whisper** is an end-to-end *encoder-decoder Transformer* for automatic speech recognition (ASR), trained by
  OpenAI on about 680,000 hours of multilingual, weakly supervised audio-text pairs. We use it **pretrained**;
  no training or fine-tuning was done.
- **Input processing:** audio is resampled to 16 kHz mono and converted to an 80-channel **log-Mel
  spectrogram** (30-second windows). The spectrogram is the "image" the network reads.
- **Encoder:** a stack of Transformer layers with self-attention turns the spectrogram into audio feature
  vectors.
- **Decoder:** an autoregressive Transformer predicts **text tokens** one at a time (sub-word BPE tokens),
  attending to the encoder output (cross-attention). Special tokens control the task: language tag,
  transcribe vs translate, timestamps.
- **Beam search (beam = 5):** instead of taking the single most probable token each step, the decoder keeps the
  5 best partial sentences and picks the best overall. This improves accuracy at some cost in speed.
- **Language identification:** if the user picks "auto", Whisper's decoder predicts the language token first;
  the probability is reported.
- **Word-level timestamps:** obtained from the decoder's **cross-attention weights** aligned with the audio
  frames using dynamic time warping. These are estimates (typically accurate to a few hundred ms).

### 2.2 Inference engine: CTranslate2 + int8 quantization
- `faster-whisper` re-implements Whisper inference on **CTranslate2**, an optimized C++ runtime.
- **Quantization:** weights are stored/computed in 8-bit integers (int8) instead of 32-bit floats. This cuts
  memory about 4x and speeds up CPU inference, with small accuracy loss.
- No PyTorch is needed at runtime (removed a multi-GB dependency).
- Runs on CPU by default; CUDA GPU supported (`auto` tries GPU and falls back to CPU). GPU was not verified on
  the development machine because the CUDA libraries are not installed.

### 2.3 Voice Activity Detection (VAD): Silero VAD
- A small neural network (Silero VAD, bundled with faster-whisper) marks which parts of the audio contain
  speech. Only speech regions are sent to Whisper.
- **Why:** Whisper is known to *hallucinate* text on silence or noise (we observed it inventing "You"). VAD
  removes that failure and also speeds up processing.

### 2.4 Non-neural components (important to say honestly)
- **FFmpeg / ffprobe:** decode any format, validate the file, convert to 16 kHz mono PCM WAV.
- **Caption segmentation:** a deterministic, rule-based algorithm (no LLM), described below.
- **Web stack:** FastAPI (Python) backend, Next.js / React / TypeScript frontend.

## 3. The pipeline, step by step

```
 Upload -> Validate -> Extract audio -> VAD -> Whisper (encoder-decoder) -> Words + timestamps
        -> Caption segmentation -> SRT / VTT files -> Browser review & edit -> Download
```

1. **Upload and validation.** The browser checks the extension and size. The server streams the file to disk,
   then runs `ffprobe` to confirm it is real media with an audio stream and acceptable duration. Bad files are
   rejected with clear error codes (empty, corrupt, no audio, too large, too long).
2. **Audio extraction.** FFmpeg converts the first audio stream to **16 kHz, mono, 16-bit PCM WAV**, the format
   Whisper expects.
3. **Job queue.** The job is queued; one worker thread processes jobs one at a time (the model already uses all
   CPU cores, so parallel jobs would only compete). Job state is saved as JSON on disk so results survive a
   restart.
4. **Voice activity detection.** Silero VAD finds speech segments (silences over 500 ms are cut).
5. **Speech recognition.** Whisper converts speech to text with word timestamps. Progress shown to the user is
   the decoded audio position divided by total duration.
6. **Caption segmentation (rule-based).** Words are grouped into captions. A new caption starts when:
   - there is a pause of 0.6 s or more, or
   - a sentence ends and the caption already has some text, or
   - the text would exceed 2 lines x 42 characters, or
   - the caption would last more than 7 s.

   Lines are balanced across two lines; minimum 1 s duration; maximum 17 characters per second reading speed;
   captions never overlap. Broken timings from the model (NaN, negative, backwards) are clamped so the output
   is always valid.
7. **Export.** SRT and WebVTT are written (UTF-8, correct millisecond rounding, escaping for VTT).
8. **Review and edit.** The UI plays the user's media with a live caption overlay; users can edit text and
   times, add or delete captions, and save. Each caption has a stable id and the job has a revision number, so
   conflicting edits are detected (HTTP 409).
9. **Download** `.srt` or `.vtt`. Files are auto-deleted after 24 h (configurable).

## 4. Architecture summary
| Layer | Technology | Role |
|---|---|---|
| Frontend | Next.js 15, React 19, TypeScript | Upload, progress, editor, preview, download |
| API | FastAPI, Uvicorn, Pydantic | Validation, job endpoints, error mapping |
| Queue | Python thread + FIFO queue, JSON files | Sequential processing, persistence |
| Media | FFmpeg / ffprobe | Validation and 16 kHz mono conversion |
| ASR | faster-whisper (CTranslate2, int8) | Transformer speech recognition |
| VAD | Silero VAD | Removes silence, prevents hallucination |
| Post-processing | Custom Python | Caption segmentation, SRT/VTT rendering |

## 5. Results and evaluation
Measured with `backend/eval/evaluate.py` on 73 human-read English utterances (LibriSpeech sample, 481 s, one
speaker), model `base`, CPU, int8. **WER** = word error rate, **CER** = character error rate, **RTF** =
processing time / audio duration (below 1 means faster than real time).

| Condition | WER | CER | RTF |
|---|---|---|---|
| Clean speech | 9.04 % | 4.25 % | 0.26 |
| Noise, SNR 10 dB | 23.13 % | 12.46 % | 0.34 |
| Noise, SNR 0 dB | 68.70 % | 42.79 % | 0.40 |
| 0.8x speed | 9.74 % | 4.81 % | 0.28 |
| 1.25x speed | 11.83 % | 5.70 % | 0.38 |

Speed example: a 2-minute clip took about 29 s on an i7-13700HX CPU.

**Takeaways:** accuracy is good on clean speech, degrades sharply with heavy noise (a known Whisper weakness),
and is only mildly affected by speaking speed. Larger models (`small`, `medium`, `large-v3`) are more accurate
but slower.

## 6. Testing
- Backend: 612 tests passing (unit, property-based, API contract, real-model, real-server and real-browser
  end-to-end). Frontend: 8 unit tests, typecheck, lint and production build pass.
- Property tests run 300 random word streams, including malformed timings, and check that captions are always
  ordered, non-overlapping and valid.
- Exports are validated with independent third-party parsers (`srt`, `webvtt-py`) and Chrome's own WebVTT parser.
- Golden-file tests pin the exact bytes of exported SRT/VTT.
- Dependencies were audited (`pip-audit`, `npm audit`) and vulnerable versions upgraded.

## 7. Limitations (state these openly)
- Accuracy measured on one speaker of read English; not representative of all accents, conversational speech or
  other languages.
- Word timestamps are estimates, so caption timing can be off by a few hundred ms.
- No speaker diarization (who is speaking), no translation, no styled subtitles (ASS).
- Heavy noise degrades accuracy severely.
- Single machine, single worker, no authentication: intended for local or trusted use.
- GPU inference was not verified on the development machine.

## 8. Possible future work
Fine-tuning or larger models for target accents, speaker diarization, translation, GPU deployment, a database
and authentication for multi-user use, and an LLM-assisted punctuation/clean-up step.

## 9. Likely questions from the examiner
- **Did you train a model?** No. We used pretrained Whisper and built the full application and post-processing
  pipeline around it.
- **Why Whisper?** Transformer ASR trained on huge multilingual data; robust to accents and noise relative to
  older models (DeepSpeech, wav2vec 2.0 needs fine-tuning); supports about 99 languages.
- **Why faster-whisper?** Same weights, 4x faster on CPU with int8, no PyTorch dependency.
- **Why VAD?** Prevents hallucination on silence and saves compute.
- **Why not an LLM for segmentation?** Rules are deterministic, testable and guarantee valid timing.
- **What is WER?** (Substitutions + deletions + insertions) / number of reference words.

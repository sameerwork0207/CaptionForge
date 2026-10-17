"""
CaptionForge real-model evaluation.

Runs the real pipeline (FFmpeg → faster-whisper → segmentation) on speech with
known reference transcripts and reports accuracy, timing validity and speed.

    python -m eval.evaluate --model base --librispeech <dir> --out eval/results

<dir> must contain LibriSpeech *.flac files plus a manifest.json:
[{"file": "1272-128104-0000.flac", "text": "MISTER QUILTER ...", "speaker_id": 1272}, ...]
(see docs/VERIFICATION_RESULTS.md for how the public sample was obtained).

Normalisation for WER/CER (documented, deliberately simple): lower-case, map
"captionforge" → "caption forge", replace every character that is not a letter,
digit, apostrophe or whitespace with a space, collapse whitespace. Numbers are
NOT converted between digits and words, so a hypothesis "2" vs reference "two"
counts as an error (this inflates WER slightly; digit tokens are reported).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import load_settings  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402
from app.services.transcription import ModelManager  # noqa: E402

SYNTHETIC_WAV = BACKEND / "tests" / "fixtures" / "speech_en.wav"
SYNTHETIC_TEXT = ("Welcome to CaptionForge. This short recording is used to test automatic "
                  "speech recognition. The quick brown fox jumps over the lazy dog. "
                  "Thank you for listening.")


# ── metrics ──────────────────────────────────────────────────────────────────

def normalise(text: str) -> List[str]:
    text = text.lower().replace("captionforge", "caption forge")
    return re.sub(r"[^\w\s']|_", " ", text).split()


def edit_distance(ref: list, hyp: list) -> int:
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(hyp)]


def peak_memory_mb() -> float | None:
    if os.name != "nt":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return None
    return c.PeakWorkingSetSize / 1024 / 1024


def cue_violations(captions, cfg) -> Dict[str, int]:
    v = {"overlap": 0, "non_positive": 0, "too_many_lines": 0, "line_too_long": 0,
         "too_long_duration": 0, "over_reading_speed": 0}
    prev_end = 0.0
    for c in captions:
        if c.start < prev_end - 1e-9:
            v["overlap"] += 1
        if c.end <= c.start:
            v["non_positive"] += 1
        lines = c.text.split("\n")
        if len(lines) > cfg.max_lines:
            v["too_many_lines"] += 1
        if any(len(l) > cfg.max_chars_per_line and " " in l for l in lines):
            v["line_too_long"] += 1
        if c.end - c.start > cfg.max_duration + 1e-6:
            v["too_long_duration"] += 1
        if len(c.text.replace("\n", "")) / max(c.end - c.start, 1e-6) > cfg.max_cps + 1e-6:
            v["over_reading_speed"] += 1
        prev_end = c.end
    return v


# ── audio conditions ─────────────────────────────────────────────────────────

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def make_condition(src: Path, dst: Path, condition: str) -> Path:
    if condition == "clean":
        return src
    if condition.startswith("noise"):
        snr = {"noise10": 10, "noise0": 0}[condition]
        # White noise scaled relative to the speech's mean volume ≈ requested SNR.
        out = subprocess.run(["ffmpeg", "-nostdin", "-i", str(src), "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        mean_db = float(re.search(r"mean_volume: (-?[\d.]+) dB", out).group(1))
        noise_db = mean_db - snr
        ffmpeg("-i", str(src), "-f", "lavfi", "-i", "anoisesrc=color=white:sample_rate=16000:amplitude=1",
               "-filter_complex",
               f"[1:a]volume={noise_db + 4.77:.2f}dB[n];[0:a][n]amix=inputs=2:duration=first:normalize=0",
               "-ar", "16000", "-ac", "1", str(dst))
        return dst
    if condition.startswith("speed"):
        factor = {"speed0.8": 0.8, "speed1.25": 1.25}[condition]
        ffmpeg("-i", str(src), "-af", f"atempo={factor}", "-ar", "16000", "-ac", "1", str(dst))
        return dst
    raise ValueError(condition)


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate_set(items, condition, settings, mm, work: Path) -> dict:
    ref_words = hyp_words = edits = ref_chars = char_edits = 0
    audio_s = wall_s = 0.0
    langs, violations, digit_tokens, per_item = {}, {}, 0, []
    for it in items:
        src = make_condition(Path(it["path"]), work / f"{condition}_{Path(it['path']).stem}.wav", condition)
        wd = Path(tempfile.mkdtemp(dir=work))
        t0 = time.perf_counter()
        res = run_pipeline(str(src), str(wd), settings, mm)
        wall = time.perf_counter() - t0
        shutil.rmtree(wd, ignore_errors=True)
        r, h = normalise(it["text"]), normalise(res.transcript)
        e = edit_distance(r, h)
        rc, hc = list(" ".join(r)), list(" ".join(h))
        ce = edit_distance(rc, hc)
        ref_words += len(r); hyp_words += len(h); edits += e
        ref_chars += len(rc); char_edits += ce
        audio_s += res.duration; wall_s += wall
        langs[res.language] = langs.get(res.language, 0) + 1
        digit_tokens += sum(any(ch.isdigit() for ch in w) for w in h)
        for k, n in cue_violations(res.captions, settings.segmentation).items():
            violations[k] = violations.get(k, 0) + n
        per_item.append({"file": Path(it["path"]).name, "wer": round(e / max(1, len(r)), 4),
                         "language": res.language, "cues": len(res.captions), "hyp": res.transcript})
    return {"condition": condition, "utterances": len(items), "ref_words": ref_words,
            "wer": round(edits / max(1, ref_words), 4), "cer": round(char_edits / max(1, ref_chars), 4),
            "languages": langs, "audio_seconds": round(audio_s, 1), "wall_seconds": round(wall_s, 1),
            "rtf": round(wall_s / max(audio_s, 1e-6), 3), "digit_tokens_in_hyp": digit_tokens,
            "violations": violations, "worst": sorted(per_item, key=lambda x: -x["wer"])[:3]}


def evaluate_long(items, settings, mm, work: Path, gap: float = 1.5) -> dict:
    """All utterances concatenated with silence gaps: boundary alignment + long-file behaviour."""
    lst, onsets, t = work / "concat.txt", [], 0.0
    silence = work / "gap.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", str(gap), "-c:a", "pcm_s16le", str(silence))
    lines = []
    for it in items:
        wav = work / f"long_{Path(it['path']).stem}.wav"
        ffmpeg("-i", it["path"], "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav))
        dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                             "format=duration", "-of", "csv=p=0", str(wav)]))
        onsets.append(t)
        t += dur + gap
        lines += [f"file '{wav.as_posix()}'", f"file '{silence.as_posix()}'"]
    lst.write_text("\n".join(lines) + "\n")
    long_wav = work / "long.wav"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(long_wav))
    wd = Path(tempfile.mkdtemp(dir=work))
    t0 = time.perf_counter()
    res = run_pipeline(str(long_wav), str(wd), settings, mm)
    wall = time.perf_counter() - t0
    ref = " ".join(it["text"] for it in items)
    r, h = normalise(ref), normalise(res.transcript)
    starts = [c.start for c in res.captions]
    aligned = sum(any(abs(s - o) <= 0.6 for s in starts) for o in onsets)
    # Captions bridging a silence gap (starting before an utterance ends and ending after
    # the next one starts) would indicate mis-merged speech.
    return {"condition": "long_concatenated", "utterances": len(items),
            "audio_seconds": round(res.duration, 1), "wall_seconds": round(wall, 1),
            "rtf": round(wall / res.duration, 3), "wer": round(edit_distance(r, h) / len(r), 4),
            "cues": len(res.captions),
            "utterance_onsets_with_cue_start_within_0.6s": f"{aligned}/{len(onsets)}",
            "violations": cue_violations(res.captions, settings.segmentation)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="base")
    ap.add_argument("--librispeech", type=Path)
    ap.add_argument("--conditions", default="clean,noise10,noise0,speed0.8,speed1.25")
    ap.add_argument("--limit", type=int, default=0, help="use only the first N utterances")
    ap.add_argument("--long", action="store_true", help="also run the concatenated long-file test")
    ap.add_argument("--out", type=Path, default=BACKEND / "eval" / "results")
    args = ap.parse_args()

    settings = load_settings({"WHISPER_MODEL": args.model, "WHISPER_DEVICE": "cpu"})
    mm = ModelManager(args.model, "cpu", "int8")
    t0 = time.perf_counter()
    mm.get()
    load_s = time.perf_counter() - t0

    report = {"model": args.model, "device": "cpu", "compute_type": "int8",
              "machine": {"cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                          "os": platform.platform(), "python": platform.python_version()},
              "model_load_seconds": round(load_s, 2), "results": []}
    work = Path(tempfile.mkdtemp(prefix="cf_eval_"))
    try:
        synthetic = [{"path": str(SYNTHETIC_WAV), "text": SYNTHETIC_TEXT}]
        r = evaluate_set(synthetic, "clean", settings, mm, work)
        r["dataset"] = "synthetic TTS (Windows SAPI, 1 clip)"
        report["results"].append(r)
        if args.librispeech:
            manifest = json.loads((args.librispeech / "manifest.json").read_text())
            items = [{"path": str(args.librispeech / m["file"]), "text": m["text"]} for m in manifest]
            if args.limit:
                items = items[:args.limit]
            for cond in args.conditions.split(","):
                r = evaluate_set(items, cond, settings, mm, work)
                r["dataset"] = "LibriSpeech dev-clean sample (human read speech, speaker 1272)"
                report["results"].append(r)
                print(f"[{args.model}] {cond}: WER {r['wer']:.2%} CER {r['cer']:.2%} RTF {r['rtf']}",
                      flush=True)
            if args.long:
                r = evaluate_long(items, settings, mm, work)
                r["dataset"] = "LibriSpeech sample concatenated with 1.5 s gaps"
                report["results"].append(r)
                print(f"[{args.model}] long: {r}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["peak_memory_mb"] = round(peak_memory_mb() or 0, 1)
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"eval_{args.model}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} (peak memory {report['peak_memory_mb']} MB)")


if __name__ == "__main__":
    main()

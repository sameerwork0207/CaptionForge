"""
Caption segmentation.

Turns Whisper output (segments with word timestamps) into readable subtitle
cues using deterministic, configurable rules (see ``SegmentationConfig``):

* at most ``max_lines`` lines of ``max_chars_per_line`` characters;
* a new cue starts after a speech pause >= ``pause_split`` seconds, after a
  sentence end, when the text no longer fits, or when the cue would exceed
  ``max_duration``; overlong cues prefer to break after a clause comma;
* cue end times are extended toward a comfortable reading speed
  (``max_cps``) and ``min_duration`` but never into the next cue;
* output timestamps are monotonic and cues never overlap.

If a segment has no word timestamps, word times are *estimated* by
distributing the segment duration proportionally to word length.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from app.config import SegmentationConfig
from app.services.transcription import RawSegment, Word

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.?!…。？！]['\"”’)\]]*$")
_CLAUSE_END = re.compile(r"[,;:，、；：–—]$")
# Hiragana/Katakana, CJK ideographs, Thai, Lao, Myanmar, Khmer.
_NO_SPACE_SCRIPT = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿฀-໿က-႟ក-៿]")


@dataclass
class Caption:
    index: int
    start: float
    end: float
    text: str   # may contain "\n" between lines


# ── Line wrapping ────────────────────────────────────────────────────────────

def wrap_text(text: str, max_chars: int, max_lines: int) -> Optional[List[str]]:
    """
    Wrap ``text`` into at most ``max_lines`` lines of ``max_chars``.

    Two-line captions are balanced (line lengths as equal as possible).
    Returns None when the text cannot fit.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return [text]
    if max_lines < 2:
        return None

    words = text.split(" ")
    if len(words) == 1:
        if not _NO_SPACE_SCRIPT.search(text):
            return None          # one long Latin/Cyrillic/... word: never break mid-word
        # Scripts written without spaces (CJK, Thai, ...): split by characters.
        lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
        return lines if len(lines) <= max_lines else None

    if max_lines == 2:
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if len(a) <= max_chars and len(b) <= max_chars:
                score = abs(len(a) - len(b)) - (3 if _CLAUSE_END.search(a) else 0)
                if best is None or score < best[0]:
                    best = (score, [a, b])
        return best[1] if best else None

    # Greedy fill for 3+ lines.
    lines, cur = [], ""
    for w in words:
        if len(w) > max_chars:
            return None
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines if len(lines) <= max_lines else None


# ── Token preparation ────────────────────────────────────────────────────────

def _estimate_words(seg: RawSegment) -> List[Word]:
    parts = seg.text.split()
    if not parts:
        return []
    total = sum(len(p) for p in parts) or 1
    dur = max(0.0, seg.end - seg.start)
    words, t = [], seg.start
    for p in parts:
        d = dur * len(p) / total
        words.append(Word(text=" " + p, start=t, end=t + d))
        t += d
    return words


def _tokens(segments: Sequence[RawSegment]) -> List[Word]:
    tokens: List[Word] = []
    last_start = 0.0
    for seg in segments:
        words = seg.words or _estimate_words(seg)
        for w in words:
            if not w.text.strip():
                continue
            # Clamp malformed model timings: non-finite or negative values and
            # timestamps that go backwards are pinned to the previous word.
            start, end = float(w.start), float(w.end)
            start = max(start, last_start) if math.isfinite(start) else last_start
            end = max(end, start) if math.isfinite(end) else start
            tokens.append(Word(text=w.text, start=start, end=end))
            last_start = start
    return tokens


def _join(tokens: Sequence[Word]) -> str:
    return re.sub(r"\s+", " ", "".join(t.text for t in tokens)).strip()


# ── Grouping ─────────────────────────────────────────────────────────────────

def _group(tokens: List[Word], cfg: SegmentationConfig) -> List[List[Word]]:
    groups: List[List[Word]] = []
    cur: List[Word] = []

    def fits(toks: Sequence[Word]) -> bool:
        return wrap_text(_join(toks), cfg.max_chars_per_line, cfg.max_lines) is not None

    for tok in tokens:
        if not cur:
            cur = [tok]
            continue

        prev_text = cur[-1].text.strip()
        cur_len = len(_join(cur))
        gap = tok.start - cur[-1].end

        if gap >= cfg.pause_split or (
            _SENTENCE_END.search(prev_text) and cur_len >= cfg.max_chars_per_line // 2
        ):
            groups.append(cur)
            cur = [tok]
            continue

        candidate = cur + [tok]
        if fits(candidate) and tok.end - cur[0].start <= cfg.max_duration:
            cur = candidate
            continue

        # Overflow: prefer breaking after a clause/sentence boundary in the
        # second half of the current cue so the remainder reads naturally.
        split_at = None
        for i in range(len(cur) - 1, len(cur) // 2 - 1, -1):
            t = cur[i].text.strip()
            if _CLAUSE_END.search(t) or _SENTENCE_END.search(t):
                split_at = i
                break
        if split_at is not None and split_at < len(cur) - 1:
            groups.append(cur[:split_at + 1])
            rest = cur[split_at + 1:] + [tok]
            if fits(rest) and tok.end - rest[0].start <= cfg.max_duration:
                cur = rest
            else:
                groups.append(cur[split_at + 1:])
                cur = [tok]
        else:
            groups.append(cur)
            cur = [tok]

    if cur:
        groups.append(cur)
    return groups


# ── Timing ───────────────────────────────────────────────────────────────────

def _fix_timing(captions: List[Caption], cfg: SegmentationConfig) -> List[Caption]:
    for i, cap in enumerate(captions):
        chars = len(cap.text.replace("\n", ""))
        desired = max(cap.end, cap.start + cfg.min_duration, cap.start + chars / cfg.max_cps)
        desired = min(desired, max(cap.end, cap.start + cfg.max_duration))
        if i + 1 < len(captions):
            limit = captions[i + 1].start - cfg.min_gap
            if limit > cap.start:
                desired = min(desired, limit)
            else:
                desired = min(desired, captions[i + 1].start)
        cap.end = desired

    # Final pass: enforce strictly positive durations and no overlaps.
    prev_end = 0.0
    for cap in captions:
        cap.start = max(cap.start, prev_end)
        if cap.end <= cap.start:
            cap.end = cap.start + 0.1
        cap.start = round(cap.start, 3)
        cap.end = round(cap.end, 3)
        prev_end = cap.end
    return captions


# ── Public API ───────────────────────────────────────────────────────────────

def build_captions(segments: Sequence[RawSegment],
                   cfg: SegmentationConfig = SegmentationConfig()) -> List[Caption]:
    """Convert transcription segments into subtitle cues."""
    tokens = _tokens(segments)
    if not tokens:
        return []
    captions = []
    for idx, group in enumerate(_group(tokens, cfg), start=1):
        text = _join(group)
        lines = wrap_text(text, cfg.max_chars_per_line, cfg.max_lines)
        # A single token longer than the line limit cannot be wrapped; keep it whole.
        captions.append(Caption(index=idx, start=group[0].start, end=group[-1].end,
                                text="\n".join(lines) if lines else text))
    return _fix_timing(captions, cfg)


class CaptionValidationError(ValueError):
    pass


def normalize_edited_captions(items: Sequence[dict]) -> List[Caption]:
    """
    Validate user-edited captions: non-negative times, end > start, non-empty
    text, no overlaps. Returns captions sorted by start and renumbered.
    """
    caps = []
    for n, item in enumerate(items, start=1):
        start, end = float(item["start"]), float(item["end"])
        text = "\n".join(l.strip() for l in str(item["text"]).replace("\r\n", "\n").split("\n")
                         if l.strip())
        if start < 0:
            raise CaptionValidationError(f"Caption {n}: start time cannot be negative.")
        if end <= start:
            raise CaptionValidationError(f"Caption {n}: end time must be after start time.")
        if not text:
            raise CaptionValidationError(f"Caption {n}: text cannot be empty.")
        caps.append(Caption(index=0, start=round(start, 3), end=round(end, 3), text=text))
    caps.sort(key=lambda c: (c.start, c.end))
    for i, c in enumerate(caps, start=1):
        c.index = i
        if i > 1 and c.start < caps[i - 2].end:
            raise CaptionValidationError(
                f"Caption {i} starts before caption {i - 1} ends (overlapping times).")
    return caps

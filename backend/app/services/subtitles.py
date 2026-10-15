"""
Subtitle export: SubRip (.srt) and WebVTT (.vtt).

Both are written as UTF-8 without a BOM. Timestamps are rounded to whole
milliseconds *before* being split into fields, so values like 59.9996 s render
as 00:01:00,000 rather than an invalid 00:00:59,1000.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List

from app.services.captions import Caption


def _split_ms(seconds: float):
    total_ms = int(round(max(0.0, seconds) * 1000))
    hrs, rem = divmod(total_ms, 3_600_000)
    mins, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return hrs, mins, secs, ms


def format_srt_timestamp(seconds: float) -> str:
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _clean_lines(text: str) -> List[str]:
    # Blank lines terminate a cue in both formats, so drop them.
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def to_srt(captions: List[Caption]) -> str:
    blocks = []
    for n, cap in enumerate(captions, start=1):
        body = "\n".join(_clean_lines(cap.text))
        blocks.append(f"{n}\n{format_srt_timestamp(cap.start)} --> "
                      f"{format_srt_timestamp(cap.end)}\n{body}\n")
    return "\n".join(blocks)


def _escape_vtt(line: str) -> str:
    line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return line.replace("-->", "--&gt;")


def to_vtt(captions: List[Caption]) -> str:
    out = ["WEBVTT", ""]
    for n, cap in enumerate(captions, start=1):
        out.append(str(n))
        out.append(f"{format_vtt_timestamp(cap.start)} --> {format_vtt_timestamp(cap.end)}")
        out.extend(_escape_vtt(line) for line in _clean_lines(cap.text))
        out.append("")
    return "\n".join(out)


FORMATS: Dict[str, dict] = {
    "srt": {"render": to_srt, "media_type": "application/x-subrip; charset=utf-8"},
    "vtt": {"render": to_vtt, "media_type": "text/vtt; charset=utf-8"},
}


def render(captions: List[Caption], fmt: str) -> str:
    renderer: Callable[[List[Caption]], str] = FORMATS[fmt]["render"]
    return renderer(captions)


def write_subtitle(captions: List[Caption], fmt: str, path: str) -> str:
    content = render(captions, fmt)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)   # atomic swap so readers never see a partial file
    return content

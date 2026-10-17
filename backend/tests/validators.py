"""
Independent subtitle validators used across the suite.

They parse with third-party libraries (``srt`` and ``webvtt-py``), not with
CaptionForge's own serializer, plus strict per-line timestamp checks that
those libraries are lenient about (e.g. ``srt`` accepts ``,1000`` ms).
"""

from __future__ import annotations

import html
import io
import re
from typing import List, Tuple

import srt
import webvtt

Cue = Tuple[float, float, str]

_SRT_LINE = re.compile(r"^(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})$")
_VTT_LINE = re.compile(r"^(\d{2}):([0-5]\d):([0-5]\d)\.(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d)\.(\d{3})$")


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _check_order(cues: List[Cue]) -> None:
    for i, (start, end, _) in enumerate(cues):
        assert end > start, f"cue {i + 1}: end <= start"
        if i:
            assert start >= cues[i - 1][1] - 1e-9, f"cue {i + 1} overlaps cue {i}"


def validate_srt(content: str) -> List[Cue]:
    """Parse SRT with the ``srt`` library and enforce strict structure."""
    assert "\r" not in content
    subs = list(srt.parse(content, ignore_errors=False))
    timing_lines = [l for l in content.split("\n") if "-->" in l]
    assert len(timing_lines) == len(subs)
    for line in timing_lines:
        assert _SRT_LINE.match(line), f"invalid SRT timing line {line!r}"
    assert [s.index for s in subs] == list(range(1, len(subs) + 1)), "bad numbering"
    cues = [(s.start.total_seconds(), s.end.total_seconds(), s.content) for s in subs]
    for _, _, text in cues:
        assert text.strip(), "empty cue text"
    _check_order(cues)
    return cues


def validate_vtt(content: str) -> List[Cue]:
    """Parse WebVTT with ``webvtt-py`` (strict) and enforce timing rules."""
    assert content.startswith("WEBVTT\n")
    doc = webvtt.from_buffer(io.StringIO(content))
    cues = []
    for c in doc.captions:
        line = f"{c.start} --> {c.end}"
        m = _VTT_LINE.match(line)
        assert m, f"invalid VTT timing {line!r}"
        g = m.groups()
        assert "-->" not in c.text
        cues.append((_secs(*g[:4]), _secs(*g[4:]), html.unescape(c.text)))
    _check_order(cues)
    return cues

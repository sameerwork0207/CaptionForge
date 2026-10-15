"""Unit tests for SRT / WebVTT export, including a strict format parser."""

import re

import pytest

from app.services.captions import Caption
from app.services.subtitles import (format_srt_timestamp, format_vtt_timestamp, to_srt, to_vtt,
                                    write_subtitle)

SRT_TS = r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
VTT_TS = r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})"


def _secs(m):
    h, mi, s, ms = map(int, m)
    assert mi < 60 and s < 60 and ms < 1000
    return h * 3600 + mi * 60 + s + ms / 1000


def parse_srt(content: str):
    """Strict SRT parser used to validate exports (raises on malformed input)."""
    cues = []
    blocks = [b for b in content.strip("\n").split("\n\n") if b]
    for n, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        assert lines[0] == str(n), f"bad sequence number {lines[0]!r}"
        m = re.fullmatch(f"{SRT_TS} --> {SRT_TS}", lines[1])
        assert m, f"bad timing line {lines[1]!r}"
        start, end = _secs(m.groups()[:4]), _secs(m.groups()[4:])
        assert end > start
        assert len(lines) >= 3, "cue without text"
        cues.append((start, end, "\n".join(lines[2:])))
    for a, b in zip(cues, cues[1:]):
        assert b[0] >= a[0], "cues out of order"
    return cues


def parse_vtt(content: str):
    assert content.startswith("WEBVTT\n\n")
    cues = []
    for block in [b for b in content[len("WEBVTT\n\n"):].strip("\n").split("\n\n") if b]:
        lines = block.split("\n")
        timing_idx = 1 if "-->" not in lines[0] else 0
        m = re.fullmatch(f"{VTT_TS} --> {VTT_TS}", lines[timing_idx])
        assert m, lines[timing_idx]
        text = "\n".join(lines[timing_idx + 1:])
        assert "-->" not in text
        cues.append((_secs(m.groups()[:4]), _secs(m.groups()[4:]), text))
    return cues


@pytest.mark.parametrize("secs,expected", [
    (0, "00:00:00,000"), (3661.5, "01:01:01,500"), (90.75, "00:01:30,750"),
    (59.9996, "00:01:00,000"), (0.0004, "00:00:00,000"), (-3, "00:00:00,000"),
    (3599.9999, "01:00:00,000"), (36000.001, "10:00:00,001"),
])
def test_srt_timestamp(secs, expected):
    assert format_srt_timestamp(secs) == expected


def test_vtt_timestamp():
    assert format_vtt_timestamp(59.9996) == "00:01:00.000"
    assert format_vtt_timestamp(1.234) == "00:00:01.234"


CAPS = [Caption(1, 0.0, 1.5, "Hello, world!\nSecond line."),
        Caption(2, 2.0, 4.25, "Ünïcödé — 日本語のテキスト ¿Sí?"),
        Caption(3, 4.25, 5.0, "a < b & c --> d")]


def test_srt_roundtrip():
    cues = parse_srt(to_srt(CAPS))
    assert [c[2] for c in cues] == [c.text for c in CAPS]
    assert cues[1][:2] == (2.0, 4.25)


def test_vtt_roundtrip_and_escaping():
    cues = parse_vtt(to_vtt(CAPS))
    assert len(cues) == 3
    assert cues[2][2] == "a &lt; b &amp; c --&gt; d"
    assert cues[1][2] == CAPS[1].text


def test_blank_lines_in_text_do_not_break_cues():
    caps = [Caption(1, 0, 1, "line one\n\n\nline two"), Caption(2, 1, 2, "next")]
    assert len(parse_srt(to_srt(caps))) == 2
    assert len(parse_vtt(to_vtt(caps))) == 2


def test_sequence_numbers_are_renumbered():
    caps = [Caption(7, 0, 1, "a"), Caption(9, 1, 2, "b")]
    assert to_srt(caps).startswith("1\n")


def test_empty_transcript_exports():
    assert to_srt([]) == ""
    assert to_vtt([]) == "WEBVTT\n"
    assert parse_vtt(to_vtt([]) + "\n") == []


def test_write_subtitle_utf8(tmp_path):
    path = tmp_path / "sub" / "captions.srt"
    write_subtitle(CAPS, "srt", str(path))
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "日本語" in raw.decode("utf-8")

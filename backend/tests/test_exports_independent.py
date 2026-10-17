"""
SRT/WebVTT exports validated by INDEPENDENT third-party parsers
(``srt``, ``webvtt-py``), over hand-picked and randomised caption sets.
"""

import math
import random

import pytest

from app.services.captions import Caption, build_captions
from app.services.subtitles import to_srt, to_vtt
from app.services.transcription import RawSegment, Word
from tests.validators import validate_srt, validate_vtt

TRICKY_TEXT = [
    "Hello, world!", "Ünïcödé — ¿qué tal? Ça va.", "日本語のテキスト", "Привет, мир", "مرحبا بالعالم",
    "a < b & c > d", "arrows --> inside", "quotes \"double\" and 'single'", "emoji 🎬🎧",
    "line one\nline two", "trailing spaces   \n   leading", "1\n00:00:01,000 --> fake",
    "x" * 300,
]


def normalise(text):
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


@pytest.mark.parametrize("text", TRICKY_TEXT)
def test_tricky_text_roundtrips_through_independent_parsers(text):
    caps = [Caption(1, 0.0, 1.0, "before"), Caption(2, 1.0, 2.5, text), Caption(3, 3.0, 4.0, "after")]
    s, v = validate_srt(to_srt(caps)), validate_vtt(to_vtt(caps))
    assert len(s) == len(v) == 3
    assert v[1][2] == normalise(text)
    assert s[1][2] == normalise(text).replace("-->", "->")    # SRT cannot escape "-->"


@pytest.mark.parametrize("seconds", [0, 0.0004, 0.0005, 0.999, 0.9995, 59.9995, 60, 3599.9995,
                                     3600, 86399.999, 359998.0])
def test_boundary_timestamps(seconds):
    caps = [Caption(1, seconds, seconds + 1.0, "x")]
    s, v = validate_srt(to_srt(caps)), validate_vtt(to_vtt(caps))
    expected = math.floor(seconds * 1000 + 0.5) / 1000      # half-up, like the frontend
    assert s[0][0] == v[0][0] == pytest.approx(expected, abs=1e-9)


def test_empty_exports():
    assert validate_srt(to_srt([])) == []
    assert validate_vtt(to_vtt([])) == []


@pytest.mark.parametrize("seed", range(40))
def test_random_pipeline_output_is_valid(seed):
    rng = random.Random(seed)
    words, t = [], 0.0
    vocab = "the quick brown fox, jumps over the lazy dog. café 日本 ünï <tag> & --> why?".split()
    for _ in range(rng.randint(1, 200)):
        d = rng.uniform(0.05, 0.6)
        words.append(Word(" " + rng.choice(vocab), t, t + d))
        t += d + rng.choice([0, 0, 0.1, 0.9, 3.0])
    caps = build_captions([RawSegment(0, t, "", words)])
    s, v = validate_srt(to_srt(caps)), validate_vtt(to_vtt(caps))
    assert len(s) == len(v) == len(caps)
    for (s0, s1, _), (v0, v1, vt), c in zip(s, v, caps):
        for got in (s0, v0):
            assert got == pytest.approx(c.start, abs=1e-9)
        for got in (s1, v1):
            assert got == pytest.approx(c.end, abs=1e-9)
        assert vt == c.text


def test_timestamps_beyond_99_hours_are_clamped():
    caps = [Caption(1, 400_000.0, 400_001.0, "x")]
    assert "99:59:59,999 --> 99:59:59,999" in to_srt(caps)

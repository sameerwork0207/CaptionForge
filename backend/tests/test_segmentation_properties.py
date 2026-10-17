"""
Property-style tests for caption segmentation: hundreds of randomised
(seeded, reproducible) word streams, including hostile timing data, checked
against invariants. Pure logic, no ML.

Invariants for every input:
  I1 cues are numbered 1..n, ordered, and never overlap
  I2 every cue has end > start >= 0 and finite times
  I3 every input word appears exactly once, in order (no text lost or duplicated)
  I4 at most ``max_lines`` lines; each line <= ``max_chars_per_line`` unless
     the line is a single word that is itself longer than the limit
  I5 cue duration <= max_duration, unless timing data makes that impossible
     (a single word lasting longer than max_duration)
"""

import math
import random

import pytest

from app.config import SegmentationConfig
from app.services.captions import build_captions
from app.services.transcription import RawSegment, Word

VOCAB = ("the a model caption forge speech audio, video. quick brown fox jumps over lazy dog! "
         "why? yes; well: extraordinarily incomprehensibilities café naïve 日本語 ünïcödé "
         "supercalifragilisticexpialidociousandthensomemore").split()


def random_stream(rng: random.Random, hostile: bool):
    segments, t = [], rng.uniform(-1, 3) if hostile else rng.uniform(0, 3)
    for _ in range(rng.randint(1, 8)):
        words = []
        for _ in range(rng.randint(1, 25)):
            w = rng.choice(VOCAB)
            dur = rng.choice([0.0, 0.01, 0.2, 0.4, 1.5]) if hostile else rng.uniform(0.1, 0.6)
            start = t
            if hostile and rng.random() < 0.15:
                start = t - rng.uniform(0, 1.0)          # out of order / overlapping
            if hostile and rng.random() < 0.05:
                start = rng.choice([math.nan, math.inf, -5.0])
            words.append(Word(" " + w, start, start + dur))
            t += dur + rng.choice([0.0, 0.05, 0.3, 0.8, 2.5])
        segments.append(RawSegment(words[0].start, words[-1].end,
                                   " ".join(w.text.strip() for w in words), words))
    return segments


CONFIGS = [SegmentationConfig(),
           SegmentationConfig(max_chars_per_line=20, max_lines=1, max_duration=3.0),
           SegmentationConfig(max_chars_per_line=32, max_lines=3, max_cps=25, pause_split=0.3)]


@pytest.mark.parametrize("hostile", [False, True], ids=["clean", "hostile"])
@pytest.mark.parametrize("cfg_idx", range(len(CONFIGS)))
@pytest.mark.parametrize("seed", range(50))
def test_invariants(seed, cfg_idx, hostile):
    cfg = CONFIGS[cfg_idx]
    rng = random.Random(seed * 7919 + cfg_idx)
    segments = random_stream(rng, hostile)
    caps = build_captions(segments, cfg)

    prev_end = 0.0
    for i, c in enumerate(caps, start=1):
        assert c.index == i                                                    # I1
        assert math.isfinite(c.start) and math.isfinite(c.end)                 # I2
        assert 0 <= c.start < c.end, c
        assert c.start >= prev_end - 1e-9, f"overlap at cue {i}"               # I1
        prev_end = c.end
        lines = c.text.split("\n")
        assert len(lines) <= cfg.max_lines, c.text                             # I4
        for line in lines:
            assert len(line) <= cfg.max_chars_per_line or " " not in line, line

    in_words = [w.text.strip() for s in segments for w in s.words]
    out_words = " ".join(c.text.replace("\n", " ") for c in caps).split()
    assert out_words == in_words                                               # I3

    if not hostile:                                                            # I5
        for c in caps:
            assert c.end - c.start <= cfg.max_duration + 1e-6, c


def test_long_unbroken_word_is_kept_whole():
    word = "x" * 120
    caps = build_captions([RawSegment(0, 2, word, [Word(" " + word, 0, 2)])])
    assert len(caps) == 1 and caps[0].text == word     # documented exception to I4


def test_very_fast_speech_cannot_meet_reading_speed():
    """30 words in 3 s: cues stay ordered and inside the speech; cps is best-effort."""
    words = [Word(" word%d" % i, i * 0.1, i * 0.1 + 0.09) for i in range(30)]
    caps = build_captions([RawSegment(0, 3, "", words)])
    assert caps[-1].end <= 3.0 + SegmentationConfig().max_duration
    for a, b in zip(caps, caps[1:]):
        assert a.end <= b.start


def test_repeated_identical_timestamps():
    words = [Word(" w%d" % i, 1.0, 1.0) for i in range(10)]
    caps = build_captions([RawSegment(1, 1, "", words)])
    assert caps and all(c.end > c.start for c in caps)


def test_all_timestamps_non_finite():
    words = [Word(" a", math.nan, math.nan), Word(" b", math.inf, math.inf)]
    caps = build_captions([RawSegment(0, 0, "a b", words)])
    assert [c.text for c in caps] == ["a b"]
    assert all(math.isfinite(c.start) and math.isfinite(c.end) for c in caps)

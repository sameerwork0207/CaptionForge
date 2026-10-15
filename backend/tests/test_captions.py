"""Unit tests for caption segmentation (pure logic, no ML)."""

import pytest

from app.config import SegmentationConfig
from app.services.captions import (CaptionValidationError, build_captions,
                                   normalize_edited_captions, wrap_text)
from app.services.transcription import RawSegment, Word

CFG = SegmentationConfig()


def words_from(text, start=0.0, step=0.3, gap_after=None):
    """Build Word tokens with even spacing; gap_after={index: extra_seconds}."""
    gap_after = gap_after or {}
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append(Word(" " + w, t, t + step * 0.9))
        t += step + gap_after.get(i, 0.0)
    return out


def seg(text, **kw):
    ws = words_from(text, **kw)
    return RawSegment(ws[0].start, ws[-1].end, text, ws)


def assert_valid(caps, cfg=CFG):
    prev_end = -1.0
    for i, c in enumerate(caps, start=1):
        assert c.index == i
        assert c.end > c.start, c
        assert c.start >= prev_end, f"overlap at caption {i}"
        prev_end = c.end
        lines = c.text.split("\n")
        assert len(lines) <= cfg.max_lines, c.text
        for line in lines:
            assert len(line) <= cfg.max_chars_per_line or " " not in line, line


def test_empty_input():
    assert build_captions([]) == []
    assert build_captions([RawSegment(0, 1, "", [])]) == []


def test_long_segment_is_split_within_limits():
    text = ("This is a very long whisper segment that keeps going and going well past the "
            "eighty four character budget for a single caption block and then continues on")
    caps = build_captions([seg(text)])
    assert len(caps) >= 2
    assert_valid(caps)
    assert " ".join(c.text.replace("\n", " ") for c in caps) == text


def test_pause_starts_new_caption_with_correct_start():
    s1 = seg("First sentence that is long enough here.", start=0.0)
    s2 = seg("Second one after a long pause.", start=10.0)
    caps = build_captions([s1, s2])
    assert len(caps) == 2
    assert caps[1].start == pytest.approx(10.0)       # not pulled back to the previous end
    assert caps[0].end < 10.0


def test_short_gap_within_sentence_keeps_words_together():
    caps = build_captions([seg("hello there my friend", gap_after={1: 0.2})])
    assert len(caps) == 1


def test_pause_inside_segment_splits():
    caps = build_captions([seg("one two three four five six", gap_after={2: 1.5})])
    assert [c.text for c in caps] == ["one two three", "four five six"]


def test_sentence_boundary_splits_once_cue_has_content():
    caps = build_captions([seg("This first sentence is reasonably long. And a second one follows.")])
    assert caps[0].text.endswith("long.")
    assert caps[1].text.startswith("And")


def test_very_short_sentence_is_not_isolated():
    caps = build_captions([seg("Hi. There we go now ok.")])
    assert len(caps) == 1


def test_max_duration_forces_split():
    # Slow speech: 1 word per second → 7 s budget forces splits even if text fits.
    caps = build_captions([seg("a b c d e f g h i j k l", step=1.0)])
    assert len(caps) >= 2
    for c in caps:
        assert c.end - c.start <= CFG.max_duration + 1e-6


def test_min_duration_and_reading_speed_extend_but_never_overlap():
    s1 = RawSegment(0, 0.2, "Okay.", [Word(" Okay.", 0.0, 0.2)])
    s2 = RawSegment(0.9, 1.2, "Next.", [Word(" Next.", 0.9, 1.2)])
    caps = build_captions([s1, s2], SegmentationConfig(pause_split=0.5))
    assert len(caps) == 2
    assert caps[0].end <= caps[1].start - CFG.min_gap + 1e-9
    assert caps[0].end > 0.2          # extended toward min duration
    assert caps[1].end - caps[1].start >= CFG.min_duration - 1e-9


def test_reading_speed_extension():
    text = "A fairly long caption line of text here"
    s = RawSegment(0, 1.0, text, [Word(" " + w, i * 0.1, i * 0.1 + 0.09)
                                  for i, w in enumerate(text.split())])
    caps = build_captions([s])
    # times are rounded to whole milliseconds (subtitle precision)
    assert caps[0].end - caps[0].start >= len(text) / CFG.max_cps - 1e-3


def test_overlapping_or_backwards_word_times_are_made_monotonic():
    ws = [Word(" one", 1.0, 1.5), Word(" two", 0.8, 1.2), Word(" three", 1.4, 1.3)]
    caps = build_captions([RawSegment(0.8, 1.5, "one two three", ws)])
    assert_valid(caps)


def test_segment_without_word_timestamps_uses_estimates():
    caps = build_captions([RawSegment(2.0, 6.0, "words without any timing information at all", [])])
    assert caps and caps[0].start == pytest.approx(2.0)
    assert caps[-1].end >= 6.0 - 1e-6
    assert_valid(caps)


def test_unicode_and_cjk_text():
    caps = build_captions([seg("Café naïve façade — ¿qué tal? Ünïcödé")])
    assert "Café" in caps[0].text and "¿qué" in caps[0].text
    cjk = "这是一个没有空格的很长的中文句子用于测试字幕分割是否正确处理没有空格的文本内容和换行规则"
    ws = [Word(ch, i * 0.2, i * 0.2 + 0.18) for i, ch in enumerate(cjk)]
    caps = build_captions([RawSegment(0, ws[-1].end, cjk, ws)])
    assert "".join(c.text.replace("\n", "") for c in caps) == cjk
    assert_valid(caps)


def test_configurable_limits():
    cfg = SegmentationConfig(max_chars_per_line=20, max_lines=1)
    caps = build_captions([seg("one two three four five six seven eight nine ten")], cfg)
    assert all(len(c.text) <= 20 and "\n" not in c.text for c in caps)


def test_clause_comma_preferred_split_point():
    text = "When the model finishes decoding the audio, we split the words into readable captions"
    caps = build_captions([seg(text)], SegmentationConfig(max_chars_per_line=30))
    assert caps[0].text.replace("\n", " ").endswith("audio,")


# ── wrap_text ────────────────────────────────────────────────────────────────

def test_wrap_balanced_two_lines():
    lines = wrap_text("The quick brown fox jumps over the lazy dog again", 42, 2)
    assert len(lines) == 2 and abs(len(lines[0]) - len(lines[1])) <= 8


def test_wrap_returns_none_when_too_long():
    assert wrap_text("x " * 60, 42, 2) is None


def test_wrap_short_text_single_line():
    assert wrap_text("  short   text ", 42, 2) == ["short text"]


# ── edited caption validation ────────────────────────────────────────────────

def test_normalize_sorts_and_renumbers():
    caps = normalize_edited_captions([{"start": 5, "end": 6, "text": "b"},
                                      {"start": 1, "end": 2, "text": " a \r\n\r\n second "}])
    assert [c.index for c in caps] == [1, 2]
    assert caps[0].text == "a\nsecond"


@pytest.mark.parametrize("items,msg", [
    ([{"start": 2, "end": 1, "text": "x"}], "end time"),
    ([{"start": -1, "end": 1, "text": "x"}], "negative"),
    ([{"start": 0, "end": 1, "text": "   "}], "empty"),
    ([{"start": 0, "end": 2, "text": "a"}, {"start": 1, "end": 3, "text": "b"}], "overlapping"),
])
def test_normalize_rejects_invalid(items, msg):
    with pytest.raises(CaptionValidationError, match=msg):
        normalize_edited_captions(items)

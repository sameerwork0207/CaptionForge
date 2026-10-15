"""Media validation / extraction with real FFmpeg (no ML)."""

import subprocess

import pytest

from app.errors import FFmpegUnavailableError, MediaError
from app.services import audio


@pytest.mark.parametrize("name,ok", [
    ("a.wav", True), ("A.MP4", True), ("x.webm", True), ("x.flac", True), ("x.m4a", True),
    ("x.txt", False), ("x", False), ("x.wav.exe", False), (".mp3.txt", False),
])
def test_is_supported(name, ok):
    assert audio.is_supported(name) is ok


def test_probe_audio_and_video(media):
    a = audio.probe_media(str(media.speech_mp3))
    assert a.has_audio and not a.has_video and a.duration == pytest.approx(12.2, abs=0.3)
    v = audio.probe_media(str(media.video_mp4))
    assert v.has_audio and v.has_video


@pytest.mark.parametrize("attr,code", [
    ("empty", "empty_file"), ("corrupt", "corrupt_media"), ("text_as_wav", "corrupt_media"),
])
def test_probe_rejects_bad_files(media, attr, code):
    with pytest.raises(MediaError) as ei:
        audio.probe_media(str(getattr(media, attr)))
    assert ei.value.code == code
    assert str(media.corrupt.parent) not in ei.value.message   # no server paths leaked


def test_validate_rejects_video_without_audio(media):
    info = audio.probe_media(str(media.video_no_audio))
    with pytest.raises(MediaError) as ei:
        audio.validate_media(info, 3600)
    assert ei.value.code == "no_audio_stream"


def test_validate_rejects_too_long(media):
    info = audio.probe_media(str(media.speech_mp3))
    with pytest.raises(MediaError) as ei:
        audio.validate_media(info, 5)
    assert ei.value.code == "media_too_long"


@pytest.mark.parametrize("attr", ["speech_mp3", "stereo_m4a", "video_mp4", "video_webm"])
def test_extract_audio_to_16k_mono(media, tmp_path, attr):
    import wave
    out = tmp_path / "out.wav"
    audio.extract_audio(str(getattr(media, attr)), str(out))
    with wave.open(str(out)) as wf:
        assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (16000, 1, 2)
    assert audio.wav_duration(str(out)) == pytest.approx(12.2, abs=0.4)


def test_extract_truncated_file_fails_cleanly(media, tmp_path):
    with pytest.raises(MediaError) as ei:
        audio.probe_media(str(media.truncated_mp4))
        audio.extract_audio(str(media.truncated_mp4), str(tmp_path / "o.wav"))
    assert ei.value.code in ("corrupt_media", "decode_failed")


def test_missing_ffmpeg(media, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert audio.ffmpeg_available() is False
    with pytest.raises(FFmpegUnavailableError):
        audio.probe_media(str(media.speech_mp3))


def test_ffmpeg_timeout(media, tmp_path, monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kw.get("timeout"))
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    with pytest.raises(MediaError) as ei:
        audio.extract_audio(str(media.speech_mp3), str(tmp_path / "o.wav"), timeout=1)
    assert ei.value.code == "media_timeout"

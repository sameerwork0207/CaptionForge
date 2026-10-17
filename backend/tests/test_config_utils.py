"""Configuration validation, filename sanitisation and model error mapping."""

import pytest

from app.config import ConfigError, load_settings
from app.errors import ModelUnavailableError
from app.services.transcription import ModelManager, split_language
from app.utils import display_filename, download_stem


def test_defaults():
    s = load_settings({})
    assert s.whisper_model == "base" and s.whisper_device == "cpu"
    assert s.whisper_compute_type == "int8"
    assert s.max_upload_bytes == 500 * 1024 * 1024
    assert s.segmentation.max_chars_per_line == 42


def test_relative_dirs_resolve_against_backend():
    s = load_settings({"UPLOAD_DIR": "tmp_up"})
    assert s.upload_dir.is_absolute() and s.upload_dir.name == "tmp_up"


@pytest.mark.parametrize("env", [
    {"WHISPER_DEVICE": "tpu"},
    {"MAX_UPLOAD_MB": "lots"},
    {"MAX_UPLOAD_MB": "0"},
    {"CAPTION_MAX_CPS": "-1"},
    {"CAPTION_MIN_DURATION": "8", "CAPTION_MAX_DURATION": "7"},
])
def test_invalid_config_rejected(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def test_cuda_default_compute_type():
    assert load_settings({"WHISPER_DEVICE": "cuda"}).whisper_compute_type == "default"


@pytest.mark.parametrize("raw,expected", [
    ("C:\\fakepath\\clip.mp4", "clip.mp4"),
    ("../../etc/passwd.wav", "passwd.wav"),
    ('we<ird>:"name|?.mp3', "we_ird_name_.mp3"),
    ("", "upload"), (None, "upload"), ("..", "upload"),
    ("vidéo été.mov", "vidéo été.mov"),
])
def test_display_filename(raw, expected):
    assert display_filename(raw) == expected


def test_download_stem():
    assert download_stem("My Talk (final).mp4") == "My_Talk_(final)"
    assert download_stem(None) == "upload"
    assert download_stem(".mp4") == "mp4"


@pytest.mark.parametrize("value,lang,err", [
    (None, None, None), ("", None, None), ("auto", None, None), ("EN", "en", None),
    ("xx", None, "Unsupported language code 'xx'."),
])
def test_split_language(value, lang, err):
    assert split_language(value) == (lang, err)


def test_unknown_model_name_maps_to_actionable_error(tmp_path):
    mm = ModelManager("not-a-real-model", "cpu", "int8", download_root=str(tmp_path))
    with pytest.raises(ModelUnavailableError, match="Unknown Whisper model"):
        mm.get()
    assert not mm.loaded


def test_long_filename_keeps_extension():
    """Regression D8: truncation used to cut off the extension → valid file rejected (415)."""
    name = display_filename("a" * 400 + ".wav")
    assert name.endswith(".wav") and len(name) == 200
    assert display_filename("b" * 400).startswith("b") and len(display_filename("b" * 400)) == 200

"""Small helpers shared by the API layer."""

from __future__ import annotations

import re
import unicodedata

_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


def display_filename(name: str | None, max_len: int = 200) -> str:
    """
    Reduce a client-supplied filename to a harmless display name: drop any
    directory components (``C:\\fakepath\\a.mp4`` → ``a.mp4``), control and
    reserved characters, and limit the length. Never used to build paths.
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE.sub("_", name).strip(" .")
    return name[:max_len] or "upload"


def download_stem(filename: str | None) -> str:
    """Base name (without extension) for downloaded subtitle files."""
    name = display_filename(filename)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = re.sub(r"\s+", "_", stem).strip("_.")
    return stem[:100] or "captions"

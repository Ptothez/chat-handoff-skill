"""Portable archive locations; never infer a user's personal workspace layout."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def relative_setting(name, default):
    value = os.environ.get(name, default).replace("\\", "/").rstrip("/")
    path = Path(value)
    if not value or path.is_absolute() or ":" in value or ".." in path.parts or value == ".":
        raise ValueError(name + " must be a nonempty workspace-relative path")
    return path.as_posix()


ARCHIVE = relative_setting("CHAT_HANDOFF_ARCHIVE", "chat-history")
BASE = relative_setting("CHAT_HANDOFF_METADATA", ".chat-handoff")
if Path(ARCHIVE).is_relative_to(BASE) or Path(BASE).is_relative_to(ARCHIVE):
    raise ValueError("Archive and metadata paths must not overlap")
REGISTRY = BASE + "/registry.json"


def configured_zone():
    name = os.environ.get("CHAT_HANDOFF_TIMEZONE")
    return ZoneInfo(name) if name else None


def relative_from_archive(path):
    return Path(os.path.relpath(path, ARCHIVE)).as_posix()


def find_root(start):
    path = Path(start).resolve()
    if path.is_file():
        path = path.parent
    for parent in (path, *path.parents):
        if (parent / REGISTRY).is_file():
            return parent
    raise ValueError("No initialized archive found; run topic_archive.py <workspace> init first")

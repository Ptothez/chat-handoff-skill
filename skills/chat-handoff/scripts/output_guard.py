"""Raw exporters create fresh incoming files, never active topics or originals."""
from datetime import datetime
from pathlib import Path

from archive_layout import ARCHIVE, configured_zone, find_root


def local_timestamp(value):
    if not value:
        return ""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = configured_zone()
    if parsed.tzinfo is None and zone is not None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


def fresh_output(value):
    path = Path(value)
    root = find_root(path.parent if path.is_absolute() else Path.cwd())
    path = (path if path.is_absolute() else root / path).resolve()
    incoming = (root / ARCHIVE / "incoming").resolve()
    if not path.is_relative_to(incoming) or path.suffix.lower() != ".md":
        raise ValueError("Export must be a fresh Markdown file inside the configured archive incoming directory")
    if path.exists():
        raise ValueError("Output exists; raw exporters never overwrite: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

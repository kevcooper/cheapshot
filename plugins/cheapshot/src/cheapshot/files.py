"""Load files named in a request, for inlining into the prompt and keying the cache by content."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class LoadedFile:
    path: str
    text: str
    sha256: str


def load(paths: list[str]) -> list[LoadedFile]:
    """Read each file as UTF-8. Raises ValueError with a message meant for the caller."""
    limit = int(os.environ.get("CHEAPSHOT_MAX_FILE_BYTES") or DEFAULT_MAX_BYTES)
    loaded, total = [], 0
    for raw in paths:
        path = Path(raw)
        # The server's working directory is the plugin, not the caller's project.
        if not path.is_absolute():
            raise ValueError(f"File paths must be absolute: {raw}")
        if not path.is_file():
            raise ValueError(f"Not a file: {raw}")
        data = path.read_bytes()
        total += len(data)
        if total > limit:
            raise ValueError(f"Files exceed {limit} bytes in total (CHEAPSHOT_MAX_FILE_BYTES).")
        try:
            text = data.decode()
        except UnicodeDecodeError:
            raise ValueError(f"Not a UTF-8 text file: {raw}") from None
        loaded.append(LoadedFile(raw, text, hashlib.sha256(data).hexdigest()))
    return loaded


def render(file: LoadedFile) -> str:
    """How a file appears to the model: its own content block, tagged with its path."""
    return f'<file path="{file.path}">\n{file.text}\n</file>'

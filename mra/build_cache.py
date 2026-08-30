"""Content fingerprints and atomic per-difficulty build metadata."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .difficulty import difficulty_output_dir


BUILD_CACHE_VERSION = 2
_MEDIA_SAMPLE_BYTES = 1024 * 1024


def sha256_file(path: str | Path) -> str | None:
    source = Path(path)
    if not source.is_file():
        return None
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: str | Path) -> dict[str, Any]:
    """Return a content fingerprint for a normal, relatively small input."""
    source = Path(path)
    if not source.is_file():
        return {"exists": False}
    stat = source.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "sha256": sha256_file(source),
    }


def media_fingerprint(path: str | Path | None) -> dict[str, Any]:
    """Fingerprint large media without hashing a multi-gigabyte file in full.

    Size and nanosecond mtime make the common path cheap.  Hashing the first and
    last MiB also catches copied-over files whose timestamp was preserved.
    """
    if path is None:
        return {"exists": False}
    source = Path(path)
    if not source.is_file():
        return {"exists": False}
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        head = stream.read(_MEDIA_SAMPLE_BYTES)
        digest.update(head)
        if stat.st_size > _MEDIA_SAMPLE_BYTES:
            stream.seek(max(0, stat.st_size - _MEDIA_SAMPLE_BYTES))
            digest.update(stream.read(_MEDIA_SAMPLE_BYTES))
    return {
        "exists": True,
        "name": source.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def fingerprint_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BuildCache:
    """Small atomic cache recording which inputs produced each artifact."""

    def __init__(self, song_dir: str | Path, difficulty: int) -> None:
        self.song_dir = Path(song_dir).resolve()
        self.difficulty = int(difficulty)
        self.path = difficulty_output_dir(self.song_dir, self.difficulty) / ".build-cache.json"
        self._data = self._load()
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        if (
            not isinstance(data, dict)
            or data.get("version") != BUILD_CACHE_VERSION
            or not isinstance(data.get("artifacts"), dict)
        ):
            return {"version": BUILD_CACHE_VERSION, "artifacts": {}}
        return data

    def matches(
        self,
        artifact: str,
        fingerprint: str,
        outputs: Iterable[str | Path],
    ) -> bool:
        paths = [Path(output) for output in outputs]
        if not paths or not all(path.is_file() for path in paths):
            return False
        entry = self._data["artifacts"].get(artifact)
        if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
            return False
        recorded_outputs = entry.get("outputs")
        if not isinstance(recorded_outputs, list) or len(recorded_outputs) != len(paths):
            return False
        for path, recorded in zip(paths, recorded_outputs):
            if not isinstance(recorded, dict):
                return False
            if recorded.get("path") != self._display_path(path):
                return False
            if recorded.get("content") != file_fingerprint(path):
                return False
        return True

    def update(
        self,
        artifact: str,
        fingerprint: str,
        outputs: Iterable[str | Path],
    ) -> None:
        paths = [Path(output) for output in outputs]
        if not paths or not all(path.is_file() for path in paths):
            return
        self._data["artifacts"][artifact] = {
            "fingerprint": fingerprint,
            "outputs": [
                {
                    "path": self._display_path(path),
                    "content": file_fingerprint(path),
                }
                for path in paths
            ],
        }
        self._dirty = True

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.song_dir).as_posix()
        except ValueError:
            return str(path.resolve())

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._dirty = False

#!/usr/bin/env python3
"""Stable JSON Lines backend used by the native desktop application."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path
from typing import Callable

from .align_audio import align_song
from .difficulty import find_preview_video, offset_file_path
from .export_player import export_player
from .init_meter import process_song as init_meter
from .make_html import generate_html
from .render_preview import record_preview, require_majdata_view
from .simai_parser import parse_maidata
from .visualize import process_song as visualize_song


def emit(event: str, **payload) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def _captured(action: Callable[[], object]) -> tuple[object, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = action()
    return result, stream.getvalue().strip()


def _run_step(index: int, total: int, name: str,
              action: Callable[[], object]) -> object:
    emit("step", name=name, status="started", index=index, total=total)
    result, details = _captured(action)
    if details:
        print(details, file=sys.stderr)
    emit("progress", value=index / total, step=name)
    emit("step", name=name, status="completed", index=index, total=total)
    return result


def export_only(song_dir: Path, difficulty: int, force: bool) -> Path:
    return export_player(song_dir, difficulty, force)


def analyze(song_dir: Path, difficulty: int, force: bool,
            timeout: int) -> Path:
    """Run the existing workflow against one exact folder, without renaming it."""
    total = 6
    _run_step(1, total, "meter", lambda: init_meter(song_dir, [difficulty]))
    stats = _run_step(
        2, total, "rhythm",
        lambda: visualize_song(song_dir, song_dir.name, force, [difficulty]),
    )
    if isinstance(stats, dict) and (stats.get("error") or stats.get("errors")):
        raise RuntimeError(stats.get("error") or "; ".join(stats["errors"]))

    def ensure_preview():
        if find_preview_video(str(song_dir), difficulty):
            return "existing"
        return record_preview(require_majdata_view(), song_dir, difficulty,
                              force=False, timeout=timeout)

    _run_step(3, total, "preview", ensure_preview)

    def ensure_alignment():
        if offset_file_path(song_dir, difficulty).is_file():
            return "existing"
        result = align_song(str(song_dir), song_dir.name, difficulty, force=False)
        if result is None:
            raise RuntimeError("audio alignment failed")
        return result

    _run_step(4, total, "alignment", ensure_alignment)
    _run_step(5, total, "html",
              lambda: generate_html(str(song_dir), song_dir.name, difficulty, 0.0))
    return _run_step(6, total, "player_export",
                     lambda: export_player(song_dir, difficulty, force))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maimai Rhythm Player backend")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "export"):
        command = subparsers.add_parser(name)
        command.add_argument("--song-dir", required=True, type=Path)
        command.add_argument("--difficulty", required=True, type=int)
        command.add_argument("--force", action="store_true")
        command.add_argument("--json-progress", action="store_true")
        command.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    song_dir = args.song_dir.expanduser().resolve()
    emit("started", command=args.command, song_dir=str(song_dir),
         difficulty=args.difficulty)
    try:
        maidata = song_dir / "maidata.txt"
        if not maidata.is_file():
            raise FileNotFoundError(f"Missing maidata.txt: {maidata}")
        song = parse_maidata(str(maidata))
        if args.difficulty not in song.charts:
            raise ValueError(f"Difficulty {args.difficulty} is not present")
        if args.command == "export":
            manifest = export_only(song_dir, args.difficulty, args.force)
        else:
            manifest = analyze(song_dir, args.difficulty, args.force, args.timeout)
        emit("completed", manifest=str(manifest), song=song.title,
             difficulty=args.difficulty)
        return 0
    except Exception as exc:
        emit("error", message=str(exc), error_type=type(exc).__name__,
             traceback=traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

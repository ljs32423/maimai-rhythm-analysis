#!/usr/bin/env python3
"""Export the shared rhythm primitives for the native desktop player."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from .difficulty import (
    DIFFICULTY_NAMES,
    default_target_difficulties,
    find_preview_video,
    legacy_difficulty_path,
    meter_file_path,
    offset_file_path,
    player_manifest_path,
    player_scene_path,
    sweep_maidata_path,
)
from .meter import ensure_meter_file
from .simai_parser import parse_maidata, time_to_beat
from .song_library import PROJECT_ROOT, find_song_dirs
from .visualize import (
    LABEL_AREA_H,
    LABEL_GAP,
    LONG_IMAGE_EXTRA_MEASURES,
    NOTE_AREA_H,
    PAD_X,
    PX_PER_BEAT,
    _prim_x_bounds,
    build_primitives,
    compute_rhythm_events,
    ensure_sweep_maidata_for_song,
    row_width_px,
)
from .sweep_marks import apply_sweep_maidata

SCHEMA_VERSION = 1


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path | None, base: Path) -> str | None:
    if path is None:
        return None
    return Path(os.path.relpath(path, base)).as_posix()


def _find_cover(song_dir: Path) -> Path | None:
    preferred = ("bg.png", "bg.jpg", "bg.jpeg", "cover.png", "cover.jpg", "jacket.png", "jacket.jpg")
    files = {path.name.casefold(): path for path in song_dir.iterdir() if path.is_file()}
    for name in preferred:
        if name in files:
            return files[name]
    for path in song_dir.iterdir():
        if path.is_file() and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
            return path
    return None


def _read_offset(song_dir: Path, difficulty: int) -> tuple[float, Path | None]:
    modern = offset_file_path(song_dir, difficulty)
    legacy = legacy_difficulty_path(song_dir, difficulty, "_offset.txt")
    path = modern if modern.is_file() else legacy if legacy.is_file() else None
    if path is None:
        return 0.0, None
    try:
        return float(path.read_text(encoding="utf-8").strip()), path
    except (OSError, ValueError):
        return 0.0, path


def _serialize_primitive(primitive: tuple, order: int) -> dict[str, Any]:
    kind = primitive[0]
    bounds = _prim_x_bounds(primitive) or (0.0, 0.0)
    result: dict[str, Any] = {
        "type": kind,
        "order": order,
        "x0": round(float(bounds[0]), 4),
        "x1": round(float(bounds[1]), 4),
    }
    if kind == "rect":
        _, x, y, width, height, fill, alpha = primitive
        result.update(x=x, y=y, width=width, height=height, fill=fill, alpha=alpha)
    elif kind == "line":
        _, x1, y1, x2, y2, stroke, stroke_width = primitive
        result.update(x1=x1, y1=y1, x2=x2, y2=y2, stroke=stroke,
                      stroke_width=stroke_width)
    elif kind == "dot":
        _, x, y, radius, fill = primitive
        result.update(x=x, y=y, radius=radius, fill=fill)
    elif kind in {"circle", "diamond"}:
        _, x, y, radius, fill, stroke, stroke_width = primitive
        result.update(x=x, y=y, radius=radius, fill=fill, stroke=stroke,
                      stroke_width=stroke_width)
    elif kind in {"ring", "diamond_ring"}:
        _, x, y, radius, stroke, stroke_width, dash = primitive
        result.update(x=x, y=y, radius=radius, stroke=stroke,
                      stroke_width=stroke_width, dash=dash)
    elif kind == "star":
        _, x, y, radius, fill = primitive
        result.update(x=x, y=y, radius=radius, fill=fill)
    elif kind == "tri":
        _, x, y, size, fill = primitive
        result.update(x=x, y=y, size=size, fill=fill)
    elif kind == "text":
        _, x, y, text, fill, font_size, weight, style, anchor = primitive
        result.update(x=x, y=y, text=str(text), fill=fill, font_size=font_size,
                      weight=weight, style=style, anchor=anchor,
                      font_family="SimHei,Microsoft YaHei,Noto Sans CJK SC,DejaVu Sans")
    else:
        raise ValueError(f"Unsupported primitive type: {kind}")
    return result


def _file_fingerprints(song_dir: Path, difficulty: int,
                       preview: Path | None, offset: Path | None) -> dict[str, Any]:
    maidata = song_dir / "maidata.txt"
    meter = meter_file_path(song_dir, difficulty)
    sweep_maidata = sweep_maidata_path(song_dir)
    result: dict[str, Any] = {
        "maidata_sha256": _sha256(maidata),
        "sweep_maidata_sha256": _sha256(sweep_maidata),
        "meter_sha256": _sha256(meter),
        "offset_sha256": _sha256(offset) if offset else None,
    }
    if preview and preview.is_file():
        stat = preview.stat()
        result["video"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    else:
        result["video"] = None
    return result


def export_player(song_dir: str | Path, difficulty: int,
                  force: bool = False) -> Path:
    """Export one exact song folder without discovering or renaming it."""
    song_root = Path(song_dir).resolve()
    maidata = song_root / "maidata.txt"
    if not maidata.is_file():
        raise FileNotFoundError(f"Missing maidata.txt: {song_root}")
    song = parse_maidata(str(maidata))
    if difficulty not in song.charts:
        raise ValueError(f"{song.title} has no difficulty {difficulty}")
    chart = song.charts[difficulty]
    if not chart.notes:
        raise ValueError(f"{song.title} difficulty {difficulty} contains no notes")
    ensure_sweep_maidata_for_song(song_root, song)

    last_note_time = max(note.time_sec + note.duration_sec for note in chart.notes)
    last_note_beat = time_to_beat(last_note_time, chart.bpm_timeline)
    meter_map = ensure_meter_file(song_root, difficulty, last_note_beat)
    folded_total_beats = meter_map.add_measures(last_note_beat, 1)
    total_beats = meter_map.add_measures(folded_total_beats, LONG_IMAGE_EXTRA_MEASURES)
    row_beats = max(1, int(math.ceil(total_beats)))

    preview_name = find_preview_video(str(song_root), difficulty)
    preview = song_root / preview_name if preview_name else None
    video_offset, offset_path = _read_offset(song_root, difficulty)
    fingerprints = _file_fingerprints(song_root, difficulty, preview, offset_path)
    manifest_path = player_manifest_path(song_root, difficulty)
    scene_path = player_scene_path(song_root, difficulty)
    if not force and manifest_path.is_file() and scene_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (existing.get("schema_version") == SCHEMA_VERSION and
                    existing.get("input_fingerprints") == fingerprints):
                return manifest_path
        except (OSError, ValueError, TypeError):
            pass

    events = compute_rhythm_events(chart)
    apply_sweep_maidata(events, song_root, difficulty)
    primitives, _ = build_primitives(
        events, row_beats, total_beats, song.bpm, chart, meter_map,
    )
    serialized = [_serialize_primitive(primitive, order)
                  for order, primitive in enumerate(primitives)]
    serialized.sort(key=lambda item: (item["x0"], item["order"]))
    scene = {
        "schema_version": SCHEMA_VERSION,
        "coordinate_system": "rhythm_strip_px",
        "width": row_width_px(row_beats),
        "height": NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H,
        "px_per_beat": PX_PER_BEAT,
        "pad_x": PAD_X,
        "total_beats": round(total_beats, 9),
        "primitive_count": len(serialized),
        "primitives": serialized,
    }

    base = manifest_path.parent
    timings = [
        {
            "time_sec": round(float(time_sec), 9),
            "beat": round(float(time_to_beat(time_sec, chart.bpm_timeline)), 9),
            "bpm": float(bpm),
        }
        for time_sec, bpm in chart.bpm_timeline
    ]
    if not timings:
        timings = [{"time_sec": 0.0, "beat": 0.0, "bpm": float(song.bpm)}]
    cover = _find_cover(song_root)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "song": {
            "title": song.title,
            "artist": song.artist,
            "genre": song.genre,
            "version": song.version,
            "folder": str(song_root),
        },
        "chart": {
            "difficulty_id": difficulty,
            "difficulty": DIFFICULTY_NAMES.get(difficulty, str(difficulty)),
            "level": str(chart.level),
            "designer": chart.designer,
        },
        "media": {
            "preview_video": _relative(preview, base),
            "cover": _relative(cover, base),
            "track": _relative(song_root / "track.mp3", base)
                     if (song_root / "track.mp3").is_file() else None,
            "duration_sec": round(last_note_time, 6),
            "video_offset_sec": round(video_offset, 6),
        },
        "timing": {
            "bpm_timeline": timings,
            "measure_boundaries": [round(value, 9)
                                   for value in meter_map.boundaries(0.0, total_beats)],
            "meter_sections": meter_map.signature_sections(),
        },
        "scene": {
            "path": "scene.json",
            "width": scene["width"],
            "height": scene["height"],
            "px_per_beat": PX_PER_BEAT,
            "pad_x": PAD_X,
        },
        "input_fingerprints": fingerprints,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    scene_path.write_text(json.dumps(scene, ensure_ascii=False, separators=(",", ":")) + "\n",
                          encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="导出原生桌面播放器数据")
    parser.add_argument("-i", "--input", default=None, help="歌曲根目录")
    parser.add_argument("-d", "--dir", default=None, help="只处理指定曲目名")
    parser.add_argument("-diff", "--difficulty", type=int, default=None, help="难度 ID")
    parser.add_argument("-f", "--force", action="store_true", help="强制重新导出")
    args = parser.parse_args()
    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f"在 {base_dir} 下未找到含 maidata.txt 的目录")
        return 1
    failures = 0
    for song_dir, song_id in songs:
        song = parse_maidata(str(Path(song_dir) / "maidata.txt"))
        difficulties = ([args.difficulty] if args.difficulty is not None else
                        default_target_difficulties(song.charts))
        for difficulty in difficulties:
            try:
                output = export_player(song_dir, difficulty, args.force)
                print(f"  [{song_id}] {DIFFICULTY_NAMES.get(difficulty, difficulty)} -> {output}")
            except Exception as exc:
                failures += 1
                print(f"  [{song_id}] {difficulty}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

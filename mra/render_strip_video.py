#!/usr/bin/env python3
"""预渲染节奏滚动条视频。

视频只承载节奏条画面，PV 仍是页面的主时钟。浏览器根据 ``offset`` 将 PV 时间
换算为谱面时间，并以轻微变速而非持续跳帧的方式保持两路视频同步。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .difficulty import (
    DIFFICULTY_NAMES,
    default_target_difficulties,
    difficulty_output_path,
    legacy_difficulty_path,
    meter_file_path,
    strip_svg_path,
    sweep_maidata_path,
)
from .ffmpeg_capabilities import require_binary
from .meter import load_meter_map
from .simai_parser import parse_maidata, time_to_beat
from .song_library import PROJECT_ROOT, find_song_dirs
from .sweep_marks import apply_sweep_maidata
from .visualize import (
    LABEL_AREA_H,
    LABEL_GAP,
    NOTE_AREA_H,
    PAD_X,
    PX_PER_BEAT,
    compute_rhythm_events,
    ensure_sweep_maidata_for_song,
    render_compact_strip_png,
    render_strip_svg,
    row_width_px,
)


# 页面中节奏条的逻辑缩放与旧 SVG/WebGL 方案保持一致。
SVG_SCALE = 1.8

# 120 fps 保证高刷屏播放时不会先天缺帧。编码画面使用 2 倍超采样，浏览器再
# 按逻辑尺寸显示：既保留旧 SVG/WebGL 在 DPR=2 屏幕上的锐度，也让 yuv420p
# 的色度分辨率在缩小后仍接近每个 CSS 像素一份颜色信息。
STRIP_VIDEO_FPS = 120
STRIP_VIDEO_SUPERSAMPLE = 2
STRIP_VIDEO_LOGICAL_WIDTH = 2048
STRIP_VIDEO_LOGICAL_HEIGHT = int(round(
    (NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H) * SVG_SCALE
))
STRIP_VIDEO_WIDTH = STRIP_VIDEO_LOGICAL_WIDTH * STRIP_VIDEO_SUPERSAMPLE
STRIP_VIDEO_HEIGHT = STRIP_VIDEO_LOGICAL_HEIGHT * STRIP_VIDEO_SUPERSAMPLE
STRIP_VIDEO_RASTER_SCALE = SVG_SCALE * STRIP_VIDEO_SUPERSAMPLE

# 2048px 视口下旧页面的判定圆位置。更窄的窗口通过平移视频适配，不缩放画面。
STRIP_VIDEO_MARKER_X = STRIP_VIDEO_LOGICAL_WIDTH * 0.168
STRIP_VIDEO_TAIL_SECONDS = 8.0
STRIP_VIDEO_FORMAT_VERSION = 3


def strip_video_path(song_dir, diff_id):
    directory = difficulty_output_path(song_dir, diff_id, "strip", "")
    metadata = load_strip_video_metadata(song_dir, diff_id)
    if metadata:
        filename = metadata.get("filename")
        if (
            isinstance(filename, str)
            and Path(filename).name == filename
            and filename.lower().endswith(".mp4")
        ):
            active = directory / filename
            if active.is_file():
                return active
    return directory / "strip_video.mp4"


def strip_video_metadata_path(song_dir, diff_id):
    return difficulty_output_path(song_dir, diff_id, "strip", "strip_video.json")


def load_strip_video_metadata(song_dir, diff_id):
    path = strip_video_metadata_path(song_dir, diff_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _source_fingerprint(song_dir: Path, diff_id: int) -> str:
    """Hash every manual/chart input that changes the rendered strip."""
    digest = hashlib.sha256()
    digest.update(json.dumps({
        "version": STRIP_VIDEO_FORMAT_VERSION,
        "difficulty": diff_id,
        "fps": STRIP_VIDEO_FPS,
        "width": STRIP_VIDEO_WIDTH,
        "height": STRIP_VIDEO_HEIGHT,
        "logical_width": STRIP_VIDEO_LOGICAL_WIDTH,
        "logical_height": STRIP_VIDEO_LOGICAL_HEIGHT,
        "supersample": STRIP_VIDEO_SUPERSAMPLE,
        "marker_x": STRIP_VIDEO_MARKER_X,
        "raster_scale": STRIP_VIDEO_RASTER_SCALE,
        "tail": STRIP_VIDEO_TAIL_SECONDS,
    }, sort_keys=True).encode("utf-8"))
    sources = (
        song_dir / "maidata.txt",
        sweep_maidata_path(song_dir),
        meter_file_path(song_dir, diff_id),
    )
    for path in sources:
        digest.update(str(path.name).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def strip_video_is_current(song_dir, diff_id, fingerprint=None):
    video_path = strip_video_path(song_dir, diff_id)
    metadata = load_strip_video_metadata(song_dir, diff_id)
    if not video_path.is_file() or not metadata:
        return False
    expected = fingerprint or _source_fingerprint(Path(song_dir), diff_id)
    return (
        metadata.get("version") == STRIP_VIDEO_FORMAT_VERSION
        and metadata.get("fingerprint") == expected
        and metadata.get("fps") == STRIP_VIDEO_FPS
        and metadata.get("width") == STRIP_VIDEO_WIDTH
        and metadata.get("height") == STRIP_VIDEO_HEIGHT
        and metadata.get("logical_width") == STRIP_VIDEO_LOGICAL_WIDTH
        and metadata.get("logical_height") == STRIP_VIDEO_LOGICAL_HEIGHT
        and metadata.get("supersample") == STRIP_VIDEO_SUPERSAMPLE
        and math.isclose(
            float(metadata.get("marker_x", -1)),
            STRIP_VIDEO_MARKER_X,
            abs_tol=1e-6,
        )
    )


def _build_scroll_timeline(chart):
    """Build chart-time to source-pixel scroll samples.

    Time zero is always chart time zero. This is important because the HTML
    subtracts the PV/audio offset before seeking this video.
    """
    timeline = chart.bpm_timeline
    chart_duration = max(
        (note.time_sec + note.duration_sec for note in chart.notes),
        default=0.0,
    )
    end_t = chart_duration + STRIP_VIDEO_TAIL_SECONDS
    key_times = {0.0, end_t}
    for time_sec, _bpm in timeline:
        if 0.0 <= time_sec <= end_t:
            key_times.add(float(time_sec))
    sample_step = 0.1
    sample_count = int(math.ceil(end_t / sample_step))
    for index in range(sample_count + 1):
        key_times.add(min(end_t, index * sample_step))

    marker_in_svg_pixels = STRIP_VIDEO_MARKER_X / SVG_SCALE
    times = sorted(key_times)
    positions = [
        time_to_beat(time_sec, timeline) * PX_PER_BEAT
        + PAD_X
        - marker_in_svg_pixels
        for time_sec in times
    ]
    return times, positions


def _frame_positions(times, positions, fps=STRIP_VIDEO_FPS):
    """Linearly interpolate exact source positions for every encoded frame."""
    if not times or len(times) != len(positions):
        raise ValueError("invalid strip-video timeline")
    end_t = times[-1]
    total_frames = max(1, int(math.ceil(end_t * fps)))
    result = []
    sample_index = 0
    for frame_index in range(total_frames):
        time_sec = frame_index / fps
        while (
            sample_index < len(times) - 2
            and times[sample_index + 1] < time_sec
        ):
            sample_index += 1
        if sample_index >= len(times) - 1:
            position = positions[-1]
        else:
            t0, t1 = times[sample_index], times[sample_index + 1]
            p0, p1 = positions[sample_index], positions[sample_index + 1]
            fraction = 0.0 if t1 <= t0 else (time_sec - t0) / (t1 - t0)
            position = p0 + (p1 - p0) * fraction
        result.append(position)
    return result


# Backward-compatible name used by the first video implementation.
_gen_ffmpeg_crop_expr = _frame_positions


def _background_frame(numpy_module):
    frame = numpy_module.empty(
        (STRIP_VIDEO_HEIGHT, STRIP_VIDEO_WIDTH, 4),
        dtype=numpy_module.uint8,
    )
    note_bottom = min(
        STRIP_VIDEO_HEIGHT,
        int(round(NOTE_AREA_H * STRIP_VIDEO_RASTER_SCALE)),
    )
    frame[:note_bottom, :, :] = (10, 10, 20, 255)
    frame[note_bottom:, :, :] = (255, 255, 255, 255)
    return frame


def _render_frames_to_video(
    strip_png_path,
    frame_positions,
    output_path,
    strip_width,
    strip_height,
    ffmpeg_bin="ffmpeg",
):
    """Stream crisp, correctly proportioned frames directly into FFmpeg."""
    import numpy as np
    from PIL import Image

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(output_path.stem + ".tmp.mp4")

    with Image.open(strip_png_path) as opened:
        strip_image = opened.convert("RGBA")
        expected_size = (
            int(round(strip_width * STRIP_VIDEO_RASTER_SCALE)),
            STRIP_VIDEO_HEIGHT,
        )
        if strip_image.size != expected_size:
            strip_image = strip_image.resize(expected_size, Image.Resampling.LANCZOS)
        strip_pixels = np.asarray(strip_image)

    image_width = strip_pixels.shape[1]
    background = _background_frame(np)
    frame = background.copy()
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-s",
        f"{STRIP_VIDEO_WIDTH}x{STRIP_VIDEO_HEIGHT}",
        "-pix_fmt",
        "rgba",
        "-r",
        str(STRIP_VIDEO_FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-tune",
        "animation",
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(STRIP_VIDEO_FPS),
        "-keyint_min",
        str(STRIP_VIDEO_FPS),
        "-sc_threshold",
        "0",
        "-movflags",
        "+faststart",
        str(temporary_output),
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    report_every = max(1, len(frame_positions) // 20)
    try:
        assert process.stdin is not None
        for frame_index, source_position in enumerate(frame_positions):
            frame[:, :, :] = background
            crop_x = int(round(source_position * STRIP_VIDEO_RASTER_SCALE))
            source_left = max(0, crop_x)
            source_right = min(image_width, crop_x + STRIP_VIDEO_WIDTH)
            if source_right > source_left:
                target_left = max(0, -crop_x)
                copy_width = source_right - source_left
                frame[:, target_left:target_left + copy_width, :] = (
                    strip_pixels[:, source_left:source_right, :]
                )
            process.stdin.write(frame.tobytes())
            if (
                (frame_index + 1) % report_every == 0
                or frame_index + 1 == len(frame_positions)
            ):
                percent = (frame_index + 1) / len(frame_positions) * 100
                print(
                    f"    strip 视频: {frame_index + 1}/"
                    f"{len(frame_positions)} ({percent:.0f}%)",
                    flush=True,
                )
        process.stdin.close()
        stderr_bytes = process.stderr.read() if process.stderr else b""
        return_code = process.wait()
        if return_code:
            message = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg 编码失败 (exit {return_code}): {message[-1000:]}"
            )
        try:
            os.replace(temporary_output, output_path)
            actual_output = output_path
        except PermissionError:
            # Windows locks a video that is currently open in a browser. Keep
            # the completed encode under a new name and let metadata switch the
            # next HTML generation to it without interrupting the old page.
            actual_output = output_path.with_name(
                f"strip_video.{time.time_ns()}.mp4"
            )
            os.replace(temporary_output, actual_output)
        return actual_output
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        temporary_output.unlink(missing_ok=True)
        raise


def render_strip_video(song_dir, diff_id=5, force=False):
    """Generate one difficulty's pre-rendered strip video."""
    song_root = Path(song_dir)
    maidata_path = song_root / "maidata.txt"
    if not maidata_path.is_file():
        print("  无 maidata.txt, 跳过")
        return None

    fingerprint = _source_fingerprint(song_root, diff_id)
    if not force and strip_video_is_current(song_root, diff_id, fingerprint):
        return None

    song = parse_maidata(str(maidata_path))
    if diff_id not in song.charts:
        print(f"  无难度 {DIFFICULTY_NAMES.get(diff_id, diff_id)}, 跳过")
        return None
    chart = song.charts[diff_id]
    if not chart.notes:
        return None

    ensure_sweep_maidata_for_song(song_root, song)
    fingerprint = _source_fingerprint(song_root, diff_id)
    output_path = strip_video_path(song_root, diff_id)

    chart_duration = max(
        (note.time_sec + note.duration_sec for note in chart.notes),
        default=0.0,
    )
    total_beats = time_to_beat(chart_duration, chart.bpm_timeline)
    meter_map = load_meter_map(song_root, diff_id, total_beats)
    rhythm_events = compute_rhythm_events(chart)
    sweep_result = apply_sweep_maidata(rhythm_events, song_root, diff_id)
    for warning in sweep_result.warnings:
        print(f"  扫键标记警告: {warning}")

    svg_path = strip_svg_path(song_root, diff_id)
    legacy_svg = legacy_difficulty_path(song_root, diff_id, "_strip.svg")
    if not svg_path.exists() and legacy_svg.exists():
        svg_path = legacy_svg
    if force or not svg_path.exists():
        svg_path = strip_svg_path(song_root, diff_id)
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        render_strip_svg(
            rhythm_events,
            total_beats,
            song.bpm,
            chart,
            str(svg_path),
            f"{song.title} ({DIFFICULTY_NAMES.get(diff_id, diff_id)})",
            row_beats=int(math.ceil(total_beats)),
            compact=True,
            meter_map=meter_map,
        )

    strip_height = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H
    strip_width = row_width_px(int(math.ceil(total_beats)))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
        png_path = Path(temporary.name)

    try:
        print(
            f"  渲染高清 strip PNG "
            f"({int(round(strip_width * STRIP_VIDEO_RASTER_SCALE))}"
            f"x{STRIP_VIDEO_HEIGHT})..."
        )
        render_compact_strip_png(
            rhythm_events,
            total_beats,
            song.bpm,
            chart,
            str(png_path),
            meter_map=meter_map,
            raster_scale=STRIP_VIDEO_RASTER_SCALE,
        )
        times, positions = _build_scroll_timeline(chart)
        frame_positions = _frame_positions(times, positions)
        print(
            f"  编码 {STRIP_VIDEO_FPS}fps / "
            f"{STRIP_VIDEO_WIDTH}x{STRIP_VIDEO_HEIGHT} / "
            f"{len(frame_positions)} 帧 → {output_path.name}..."
        )
        actual_output = _render_frames_to_video(
            png_path,
            frame_positions,
            output_path,
            strip_width,
            strip_height,
            require_binary("ffmpeg"),
        )
        metadata = {
            "version": STRIP_VIDEO_FORMAT_VERSION,
            "fingerprint": fingerprint,
            "fps": STRIP_VIDEO_FPS,
            "width": STRIP_VIDEO_WIDTH,
            "height": STRIP_VIDEO_HEIGHT,
            "logical_width": STRIP_VIDEO_LOGICAL_WIDTH,
            "logical_height": STRIP_VIDEO_LOGICAL_HEIGHT,
            "supersample": STRIP_VIDEO_SUPERSAMPLE,
            "marker_x": STRIP_VIDEO_MARKER_X,
            "duration": len(frame_positions) / STRIP_VIDEO_FPS,
            "filename": actual_output.name,
        }
        metadata_path = strip_video_metadata_path(song_root, diff_id)
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for obsolete in actual_output.parent.glob("strip_video*.mp4"):
            if obsolete == actual_output:
                continue
            try:
                obsolete.unlink()
            except OSError:
                # It may still be held by an already-open browser page.
                pass
        print(f"  → {actual_output}")
        return str(actual_output)
    finally:
        png_path.unlink(missing_ok=True)


def available_chart_difficulties(song_dir):
    maidata = Path(song_dir) / "maidata.txt"
    if not maidata.is_file():
        return []
    return default_target_difficulties(parse_maidata(str(maidata)).charts)


def main():
    parser = argparse.ArgumentParser(description="预渲染节奏滚动条视频")
    parser.add_argument("-i", "--input", default=None, help="歌曲根目录")
    parser.add_argument("-d", "--dir", default=None, help="只处理指定曲目名")
    parser.add_argument(
        "-diff",
        "--difficulty",
        type=int,
        default=None,
        help="难度 ID；不指定则默认只处理 MASTER/Re:MASTER",
    )
    parser.add_argument("-f", "--force", action="store_true", help="强制重新生成")
    args = parser.parse_args()

    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    if not os.path.isdir(base_dir):
        print(f"错误: {base_dir} 不存在")
        return 1
    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f"在 {base_dir} 下未找到含 maidata.txt 的目录")
        return 1

    difficulty_label = (
        DIFFICULTY_NAMES.get(args.difficulty, args.difficulty)
        if args.difficulty is not None
        else "默认 MASTER/Re:MASTER"
    )
    print(f"发现 {len(songs)} 首歌曲, {difficulty_label}\n")
    failures = 0
    for song_dir, song_id in songs:
        difficulties = (
            [args.difficulty]
            if args.difficulty is not None
            else available_chart_difficulties(song_dir)
        )
        if not difficulties:
            print(f"  [{song_id}] 未发现可生成的谱面难度")
            failures += 1
            continue
        for difficulty in difficulties:
            try:
                result = render_strip_video(
                    song_dir,
                    difficulty,
                    force=args.force,
                )
                if result is None:
                    print(
                        f"  [{song_id}] "
                        f"{DIFFICULTY_NAMES.get(difficulty, difficulty)} "
                        "strip 视频已是最新"
                    )
            except Exception as exc:
                import traceback

                print(
                    f"  [{song_id}] "
                    f"{DIFFICULTY_NAMES.get(difficulty, difficulty)} ✗ {exc}"
                )
                traceback.print_exc()
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

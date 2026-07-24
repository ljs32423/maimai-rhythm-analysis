#!/usr/bin/env python3
"""
maimai 节奏解析网页生成器
=============================
生成包含谱面预览视频 + Arcaea 风格分段 SVG 节奏条的 HTML 页面。

布局: 上方谱面视频 / 下方 GPU 合成滚动的分段 SVG
同步: PV 是主时钟，offset 由 align_audio.py 生成；单调时钟连续驱动 SVG 位移

用法:
  python make_html.py                          # 批量所有歌曲
  python make_html.py -d "WiPE OUT MEMORIES"   # 单曲
  python make_html.py -diff 4 -f               # 指定难度+强制
"""
import os, sys, argparse, html, json, re
import math
from pathlib import Path
from urllib.parse import quote

from .simai_parser import parse_maidata, time_to_beat
from .meter import load_meter_map
from .visualize import (compute_rhythm_events, PX_PER_BEAT, PAD_X,
                        NOTE_AREA_H, LABEL_GAP, LABEL_AREA_H, NOTE_CY,
                        NOTE_OUTER_DIAMETER, SEGMENT_BEATS,
                        ensure_sweep_maidata_for_song,
                        render_strip_svg_segments)
from .difficulty import (DIFFICULTY_NAMES, analysis_html_path, default_target_difficulties,
                         difficulty_file_stem, find_preview_video,
                         legacy_difficulty_path,
                         offset_file_path, preview_video_candidates,
                         strip_segment_base_path, strip_svg_path)
from .song_library import PROJECT_ROOT, find_song_dirs
from .sweep_marks import apply_sweep_maidata

# Arcaea 常量 (与 4.py 完全一致)
# 降低滚动条整体缩放，等价于降低屏幕上“每拍经过的像素数”，从而减慢观感滚动速度。
SVG_SCALE = 1.8
PLAYER_RENDERER_VERSION = 6


ANALYSIS_SERVER_SCRIPT = r'''#!/usr/bin/env python3
"""Start a short-lived local server for the generated analysis page."""
from __future__ import annotations

import time
import webbrowser
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SERVE_ROOT = SCRIPT_DIR.parent
IDLE_TIMEOUT_SECONDS = int(os.environ.get("MRA_ANALYSIS_IDLE_TIMEOUT", 30 * 60))
last_activity = time.monotonic()


class AnalysisHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SERVE_ROOT), **kwargs)

    def _touch(self):
        global last_activity
        last_activity = time.monotonic()

    def _send_range(self, head_only=False):
        range_header = self.headers.get("Range")
        if not range_header:
            return False
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return False
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            self.send_error(416, "Invalid byte range")
            return True
        size = path.stat().st_size
        start_text, end_text = match.groups()
        if not start_text:
            length = int(end_text or 0)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = min(size - 1, int(end_text)) if end_text else size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return True
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if head_only:
            return True
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return True

    def do_GET(self):
        self._touch()
        if not self._send_range():
            super().do_GET()

    def do_HEAD(self):
        self._touch()
        if not self._send_range(head_only=True):
            super().do_HEAD()

    def log_message(self, _format, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), AnalysisHandler)
server.daemon_threads = True
server.timeout = 1
port = server.server_address[1]
url = f"http://127.0.0.1:{port}/html/analysis.html"
if os.environ.get("MRA_ANALYSIS_NO_BROWSER") == "1":
    print(url, flush=True)
else:
    webbrowser.open(url, new=2)
try:
    while time.monotonic() - last_activity < IDLE_TIMEOUT_SECONDS:
        server.handle_request()
finally:
    server.server_close()
'''


ANALYSIS_LAUNCHER_CMD = r'''@echo off
setlocal
set "SERVER_SCRIPT=%~dp0_open_analysis_server.py"

where pythonw.exe >nul 2>&1
if errorlevel 1 goto try_pyw
start "" pythonw.exe "%SERVER_SCRIPT%"
exit /b 0

:try_pyw
where pyw.exe >nul 2>&1
if errorlevel 1 goto try_python
start "" pyw.exe -3 "%SERVER_SCRIPT%"
exit /b 0

:try_python
where python.exe >nul 2>&1
if errorlevel 1 goto no_python
start "maimai rhythm analysis" /min python.exe "%SERVER_SCRIPT%"
exit /b 0

:no_python
echo Python was not found. Install Python or add it to PATH.
pause
exit /b 1
'''


def write_analysis_launcher(html_dir: Path) -> tuple[Path, Path]:
    server_path = html_dir / "_open_analysis_server.py"
    launcher_path = html_dir / "打开分析页面.cmd"
    server_path.write_text(ANALYSIS_SERVER_SCRIPT, encoding="utf-8", newline="\n")
    launcher_path.write_text(ANALYSIS_LAUNCHER_CMD, encoding="ascii", newline="\r\n")
    return launcher_path, server_path


def build_timing_segments(chart):
    """Build piecewise timing data for precise variable-BPM playback."""
    timeline = chart.bpm_timeline
    return [
        {
            'beat': round(time_to_beat(time_sec, timeline), 9),
            'bpm': bpm,
            'time': round(time_sec, 9),
        }
        for time_sec, bpm in timeline
    ]


def generate_html(song_dir, song_id, diff_id=5, offset=0.0):
    song_root = Path(song_dir)
    out_path = analysis_html_path(song_root, diff_id)
    html_dir = out_path.parent
    html_dir.mkdir(parents=True, exist_ok=True)

    def asset_url(path: str | Path) -> str:
        rel = os.path.relpath(Path(path), html_dir).replace(os.sep, '/')
        return quote(rel, safe='/')

    maidata = os.path.join(song_dir, 'maidata.txt')
    diff_name = DIFFICULTY_NAMES.get(diff_id, diff_id)
    file_stem = difficulty_file_stem(diff_id)
    found_pv_name = find_preview_video(song_dir, diff_id)
    pv_candidates = preview_video_candidates(diff_id)
    pv_rel_names = list(dict.fromkeys(([found_pv_name] if found_pv_name else []) + pv_candidates))
    pv_name = found_pv_name or pv_candidates[0]
    pv_paths = [song_root / name for name in pv_rel_names]
    svg_path = strip_svg_path(song_root, diff_id)
    legacy_svg_path = legacy_difficulty_path(song_root, diff_id, '_strip.svg')
    if not svg_path.exists() and legacy_svg_path.exists():
        svg_path = legacy_svg_path
    svg_name = asset_url(svg_path)

    # 自动读取对齐偏移
    offset_file = offset_file_path(song_root, diff_id)
    legacy_offset_file = legacy_difficulty_path(song_root, diff_id, '_offset.txt')
    if not offset_file.exists() and legacy_offset_file.exists():
        offset_file = legacy_offset_file
    auto_offset = offset
    if os.path.exists(offset_file):
        try:
            with open(offset_file, 'r') as f:
                auto_offset = float(f.read().strip())
            print(f'  [{song_id}] 自动对齐 offset={auto_offset:+.3f}s')
        except:
            pass
    if offset != 0.0:
        auto_offset = offset

    if not os.path.exists(maidata):
        print(f'  [{song_id}] 无 maidata.txt'); return
    if not os.path.exists(svg_path):
        print(f'  [{song_id}] 无 {svg_path.relative_to(song_root) if svg_path.is_relative_to(song_root) else svg_path}, 请先运行 visualize.py -f'); return

    song = parse_maidata(maidata)
    sweep_path, sweep_created = ensure_sweep_maidata_for_song(song_root, song)
    if sweep_created:
        print(f'  [{song_id}] 已创建人工扫键标记文件 {sweep_path.name}')
    if diff_id not in song.charts:
        print(f'  [{song_id}] 无难度 {diff_name}'); return
    ch = song.charts[diff_id]
    if not ch.notes:
        return
    rhythm_events = compute_rhythm_events(ch)
    sweep_result = apply_sweep_maidata(rhythm_events, song_root, diff_id)
    if sweep_result.created:
        print(f'  [{song_id}] 已创建人工扫键标记文件 {sweep_result.path.name}')
    for warning in sweep_result.warnings:
        print(f'  [{song_id}] 扫键标记警告: {warning}')

    bpm = song.bpm
    bpm_values = [value for _, value in ch.bpm_timeline] or [bpm]
    bpm_min, bpm_max = min(bpm_values), max(bpm_values)
    bpm_range = (f'{bpm_min:g}' if math.isclose(bpm_min, bpm_max)
                 else f'{bpm_min:g} – {bpm_max:g}')
    chart_duration = max((note.time_sec + note.duration_sec for note in ch.notes), default=0.0)
    duration_text = f'{int(chart_duration // 60)}:{int(chart_duration % 60):02d}'
    total_beats = time_to_beat(chart_duration, ch.bpm_timeline)
    meter_map = load_meter_map(song_root, diff_id, total_beats)
    first_note_time = min(note.time_sec for note in ch.notes)
    first_note_beat = time_to_beat(first_note_time, ch.bpm_timeline)
    measure_boundaries = meter_map.numbered_boundaries(first_note_beat, total_beats)
    if not measure_boundaries:
        measure_boundaries = [first_note_beat]
    meter_sections = [
        {"start_beat": section["start_beat"], "signature": section["signature"]}
        for section in meter_map.signature_sections()
    ]
    if not meter_sections:
        meter_sections = [{"start_beat": 0.0, "signature": "4/4"}]

    # timing 数据
    timings_js = build_timing_segments(ch)
    if not timings_js:
        timings_js.append({'beat': 0, 'bpm': bpm, 'time': 0})
    start_candidates = meter_map.boundaries(-16.0, timings_js[0]['beat'])
    start_display_beat = start_candidates[-1] if start_candidates else 0.0
    if float(start_display_beat).is_integer():
        start_display_beat = int(start_display_beat)

    mime_types = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska'}
    video_sources = '\n'.join(
        f'        <source src="{html.escape(asset_url(path))}" '
        f'type="{mime_types.get(path.suffix.lower(), "video/mp4")}">'
        for path in pv_paths
    )

    # 将超长 SVG 切成较短的独立图片。每段保持全局 viewBox 坐标，前端只需
    # 平移一个父合成层；分段本身在播放前全部加载、解码并固定挂载。
    with open(svg_path, 'r', encoding='utf-8') as f:
        svg_head = f.read(500)
    width_match = re.search(r'width="([0-9.]+)"', svg_head)
    svg_w = int(math.ceil(float(width_match.group(1)))) if width_match else 30000
    svg_h = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H
    segment_width = SEGMENT_BEATS * PX_PER_BEAT
    row_beats = max(0.0, (svg_w - PAD_X * 2) / PX_PER_BEAT)
    expected_segment_count = (
        max(1, int(math.ceil(row_beats / SEGMENT_BEATS)))
        if row_beats > 0 else 0
    )

    def find_segments(directory, pattern):
        if not directory.is_dir():
            return []
        found = []
        for name in os.listdir(directory):
            match = pattern.match(name)
            if match:
                found.append((int(match.group(1)), name))
        found.sort()
        return found

    modern_segment_dir = strip_segment_base_path(song_root, diff_id).parent
    modern_segment_re = re.compile(r'^strip_seg_([0-9]{3})[.]svg$')
    legacy_segment_re = re.compile(
        rf'^{re.escape(file_stem)}_strip_seg_([0-9]{{3}})[.]svg$'
    )

    def segments_are_complete(found):
        return [index for index, _name in found] == list(
            range(expected_segment_count)
        )

    segment_dir = modern_segment_dir
    found_segments = find_segments(segment_dir, modern_segment_re)
    if not segments_are_complete(found_segments):
        segment_dir = song_root
        found_segments = find_segments(segment_dir, legacy_segment_re)

    if not segments_are_complete(found_segments):
        segment_dir = modern_segment_dir
        segment_base = strip_segment_base_path(song_root, diff_id)
        try:
            segment_base.parent.mkdir(parents=True, exist_ok=True)
            render_strip_svg_segments(
                rhythm_events,
                row_beats,
                song.bpm,
                ch,
                str(segment_base),
                meter_map=meter_map,
            )
            found_segments = find_segments(segment_dir, modern_segment_re)
            if segments_are_complete(found_segments):
                print(
                    f'  [{song_id}] 已生成 {len(found_segments)} 个高性能分段 SVG'
                )
        except Exception as exc:
            print(f'  [{song_id}] 分段 SVG 生成失败，将使用完整 SVG: {exc}')

    segments_js = []
    if segments_are_complete(found_segments):
        for index, name in found_segments:
            if index == 0:
                x = 0
                width = min(svg_w, PAD_X + segment_width)
            else:
                x = PAD_X + index * segment_width
                width = min(segment_width, max(1, svg_w - x))
            segments_js.append({
                'src': asset_url(segment_dir / name),
                'x': round(x, 3),
                'width': round(width, 3),
            })

    if segments_js:
        scrolling_svg_html = (
            f'<object class="scrolling-svg" id="svgScroll" '
            f'type="image/svg+xml" width="{svg_w}" height="{svg_h}" '
            f'hidden></object>'
        )
    else:
        scrolling_svg_html = (
            f'<object class="scrolling-svg" id="svgScroll" '
            f'data="{svg_name}" type="image/svg+xml" '
            f'width="{svg_w}" height="{svg_h}"></object>'
        )

    # 网页滚动单位: 每拍 = PX_PER_BEAT * SVG_SCALE (屏幕像素)。
    rhythm_height = int(math.ceil(svg_h * SVG_SCALE))
    pentagon_width = int(round(rhythm_height * (173.2 / 130.8)))
    pentagon_white_shift = round(rhythm_height * (7.2 / 130.8), 1)
    pentagon_dark_shift = round(rhythm_height * (18.0 / 130.8), 1)
    marker_size = int(round((NOTE_OUTER_DIAMETER + 2) * SVG_SCALE))
    marker_top = round(NOTE_CY * SVG_SCALE - marker_size / 2, 1)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(song.title)} — 节奏解析</title>
<style>
:root {{
    --rhythm-height: {rhythm_height}px;
    --play-position: 16.8%;
    --marker-size: {marker_size}px;
    --marker-top: {marker_top}px;
    --pentagon-width: {pentagon_width}px;
}}
* {{ box-sizing: border-box; }}
body {{
    margin: 0; overflow: hidden; min-height: 100vh;
    background: #000;
    font-family: 'Microsoft YaHei', sans-serif;
    display: flex; flex-direction: column;
    color: #f5f5f7; user-select: none;
}}
/* 视频区 */
.video-area {{
    position: relative; width: 100vw; height: calc(100vh - var(--rhythm-height));
    min-height: 0;
    padding-left: clamp(4px, 0.5vw, 8px);
    padding-right: clamp(24px, 4vw, 56px);
    background: #000;
    display: grid; grid-template-columns: minmax(0, 1fr) clamp(430px, 34vw, 820px);
    overflow: hidden;
}}
.video-area::before {{
    content: ''; position: absolute; inset: 0; z-index: 0; pointer-events: none;
    background: #000;
}}
.video-pane {{
    position: relative; z-index: 1; min-width: 0; min-height: 0; overflow: hidden;
    display: flex; align-items: center; justify-content: center;
    background: #000;
}}
.video-crop {{
    position: relative;
    width: min(100%, calc(100vh - var(--rhythm-height)));
    aspect-ratio: 1 / 1;
    overflow: hidden;
    background: #000;
    flex: 0 0 auto;
}}
.video-crop video {{
    position: absolute;
    inset: 0;
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    object-position: center center;
    transform: none;
}}
.info-pane {{
    position: relative; z-index: 1; min-width: 0; min-height: 0;
    display: flex; flex-direction: column; justify-content: center;
    padding: clamp(24px, 2.2vw, 44px);
    background: transparent;
}}
.song-meta {{
    padding: 0 0 18px; min-width: 0;
    border-bottom: 1px solid rgba(255,255,255,0.11);
}}
.song-meta-body {{ display: flex; flex-direction: column; gap: 7px; min-width: 0; }}
.song-title {{
    font-size: clamp(23px, 2vw, 36px); font-weight: 800; line-height: 1.15;
    letter-spacing: 0;
    overflow-wrap: anywhere;
}}
.song-difficulty {{
    color: rgba(206, 211, 225, 0.82); font-size: 13px; font-weight: 700;
    letter-spacing: 0.04em;
}}
.chart-details {{
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    column-gap: 22px; margin-top: 12px;
}}
.detail-item {{
    min-width: 0; padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,0.075);
}}
.detail-item span {{
    display: block; margin-bottom: 3px; color: rgba(151,157,177,0.76);
    font-size: 10px; font-weight: 700; letter-spacing: 0.08em;
}}
.detail-item strong {{
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: rgba(241,243,249,0.94); font-size: 13px; font-weight: 700;
}}
.video-empty {{
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    color: #858791; text-align: center; font-size: 14px;
}}
.video-empty[hidden] {{ display: none; }}
.video-empty strong {{ display: block; color: #d3d4da; margin-bottom: 5px; font-size: 16px; }}
.local-file-warning {{
    position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
    z-index: 100; max-width: min(92vw, 720px); padding: 9px 14px;
    border: 1px solid rgba(255, 193, 7, 0.72); border-radius: 9px;
    color: #fff4cc; background: rgba(46, 35, 3, 0.94);
    box-shadow: 0 6px 24px rgba(0,0,0,0.35);
    font-size: 12px; font-weight: 700; text-align: center;
}}
.local-file-warning[hidden] {{ display: none; }}

/* Arcaea 风格节奏条区 */
.rhythm-container {{
    position: relative; width: 100vw; height: var(--rhythm-height);
    flex: 0 0 var(--rhythm-height);
    overflow: hidden;
    background:
        linear-gradient(to bottom,
            #0a0a14 0,
            #0a0a14 {NOTE_AREA_H * SVG_SCALE}px,
            #ffffff {NOTE_AREA_H * SVG_SCALE}px,
            #ffffff 100%);
    border-top: none;
    box-shadow: none;
}}
.rhythm-container::before {{
    content: '';
    position: absolute; inset: 0;
    display: none;
    background: none;
    pointer-events: none;
    z-index: 0;
}}
.svg-container {{
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    overflow: hidden;
    z-index: 1;
    contain: strict;
}}
.scrolling-stage {{
    position: absolute; top: 0; left: 0;
    width: {svg_w}px; height: {svg_h}px;
    transform: scale({SVG_SCALE});
    transform-origin: top left;
    contain: layout paint style;
}}
.virtual-strip,
.scrolling-svg {{
    position: absolute; top: 0; left: 0;
    width: {svg_w}px; height: {svg_h}px;
    transform: translate3d(0, 0, 0);
    will-change: transform;
    contain: layout paint style;
    backface-visibility: hidden;
    z-index: 1;
}}
.virtual-strip {{
    isolation: isolate;
}}
.svg-segment {{
    position: absolute; top: 0;
    height: {svg_h}px;
    display: block;
    max-width: none;
    pointer-events: none;
    user-select: none;
    image-rendering: auto;
    contain: strict;
}}
.left-pentagon {{
    position: absolute; top: 0; width: var(--pentagon-width); height: var(--rhythm-height);
    clip-path: polygon(0% 0%, 87.5% 0%, 100% 50%, 87.5% 100%, 0% 100%);
    pointer-events: none;
}}
.play-marker {{
    position: absolute; left: calc(var(--play-position) - (var(--marker-size) / 2)); top: var(--marker-top);
    width: var(--marker-size); height: var(--marker-size); border: 2px solid #fff; border-radius: 50%;
    z-index: 9; box-shadow: 0 0 0 1px rgba(0,0,0,0.72);
    pointer-events: none;
}}
.bpm-readout {{
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    width: calc(var(--pentagon-width) - 20px);
    text-align: center; color: #fff;
    font-family: 'Noto Sans ExtraCondensed', 'Consolas', sans-serif;
    z-index: 6;
    pointer-events: none;
}}
.bpm-readout span {{
    display: block; font-size: 11px; line-height: 1; font-weight: 800;
    letter-spacing: 0.18em; color: rgba(255,255,255,0.92);
}}
.bpm-readout strong {{
    display: block; margin-top: 5px; font-size: 34px; line-height: 0.95; font-weight: 700;
    font-variant-numeric: tabular-nums;
}}

/* 控制面板 */
.controls {{
    z-index: 10; width: min(100%, 720px);
    margin-top: clamp(14px, 1.4vw, 24px);
    font-size: 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    padding: 11px 12px;
    border-radius: 14px;
    background: rgba(7,9,17,0.62);
    border: 1px solid rgba(126, 136, 170, 0.14);
    backdrop-filter: blur(8px);
    box-shadow: 0 12px 30px rgba(0,0,0,0.22);
}}
.control-buttons {{
    display: flex; gap: 4px; align-items: center; flex: 0 0 auto;
}}
.measure-status {{
    --status-number-size: 14px;
    display: flex; align-items: baseline; gap: 4px; flex: 0 0 auto;
    min-width: 82px; height: 28px; padding: 4px 10px;
    border-radius: 999px;
    color: rgba(226,229,240,0.9);
    background: linear-gradient(135deg, rgba(108,92,231,0.22), rgba(79,195,247,0.12));
    border: 1px solid rgba(145,132,255,0.32);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 3px 12px rgba(0,0,0,0.16);
    font-size: 9px; font-weight: 700; white-space: nowrap;
}}
.measure-status strong {{
    color: #fff; font-family: Consolas, monospace; font-size: var(--status-number-size);
    line-height: 1; font-variant-numeric: tabular-nums;
}}
.measure-status em {{
    color: rgba(191,195,211,0.68); font-family: Consolas, monospace;
    font-size: var(--status-number-size); line-height: 1;
    font-style: normal; font-variant-numeric: tabular-nums;
}}
.meter-status {{
    --status-number-size: 14px;
    display: flex; align-items: baseline; gap: 5px; flex: 0 0 auto;
    min-width: 68px; height: 28px; padding: 4px 10px;
    border-radius: 999px;
    color: rgba(226,229,240,0.9);
    background: linear-gradient(135deg, rgba(0,184,148,0.2), rgba(79,195,247,0.1));
    border: 1px solid rgba(85,215,184,0.28);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 3px 12px rgba(0,0,0,0.16);
    font-size: 9px; font-weight: 700; white-space: nowrap;
}}
.meter-status strong {{
    color: #fff; font-family: Consolas, monospace; font-size: var(--status-number-size);
    line-height: 1; font-variant-numeric: tabular-nums;
}}
.speed-delay-group {{
    display: flex; gap: 10px; align-items: center; flex: 0 0 auto;
}}
.controls button {{
    background: rgba(20,22,34,0.72); color: rgba(235, 237, 244, 0.96); border: 1px solid rgba(110, 116, 140, 0.32); border-radius: 999px;
    width: 26px; height: 26px; padding: 0; cursor: pointer; font-size: 11px; transition: all 0.18s ease;
    box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}}
.controls button:hover {{ background: rgba(42, 44, 62, 0.9); border-color: rgba(120, 124, 144, 0.72); }}
.controls button:disabled {{ opacity: 0.35; cursor: default; }}
.controls input[type=range] {{ width: 100px; }}
.controls input[type=range] {{
    appearance: none; height: 2px; border-radius: 999px;
    background: rgba(108, 112, 132, 0.32); outline: none;
}}
.controls input[type=range]::-webkit-slider-thumb {{
    appearance: none; width: 12px; height: 12px; border-radius: 50%;
    background: #f5f5f7; border: 1px solid #11131a; cursor: pointer;
    box-shadow: 0 0 0 2px rgba(0,0,0,0.18);
}}
.controls input[type=range]::-moz-range-thumb {{
    width: 12px; height: 12px; border-radius: 50%;
    background: #f5f5f7; border: 1px solid #11131a; cursor: pointer;
    box-shadow: 0 0 0 2px rgba(0,0,0,0.18);
}}
.controls input[type=range]::-moz-range-track {{
    height: 2px; border-radius: 999px; background: rgba(108, 112, 132, 0.32);
}}
.seek-wrap {{
    display: flex; align-items: center; gap: 8px; position: relative;
    min-width: 0; flex: 1 1 250px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(20,22,34,0.42);
    border: 1px solid rgba(102, 106, 126, 0.22);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.02), 0 1px 6px rgba(0,0,0,0.14);
    backdrop-filter: blur(2px);
    transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}}
.speed-wrap {{
    display: flex; align-items: center; gap: 6px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(20,22,34,0.42);
    border: 1px solid rgba(102, 106, 126, 0.22);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.02), 0 1px 6px rgba(0,0,0,0.14);
    backdrop-filter: blur(2px);
    transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}}
.speed-wrap:hover {{
    background: rgba(14,14,24,0.64);
    border-color: rgba(92, 96, 116, 0.5);
    box-shadow: 0 3px 12px rgba(0,0,0,0.24);
}}
.speed-label {{
    color: rgba(214, 217, 227, 0.92);
    font-size: 10px;
    font-weight: 700;
    white-space: nowrap;
}}
.speed-wrap input[type=range] {{
    width: 74px; min-width: 74px;
    --speed-progress: 42.8571428571%;
    background: linear-gradient(to right, #ff8a65 0%, #ff8a65 var(--speed-progress), rgba(108, 112, 132, 0.32) var(--speed-progress), rgba(108, 112, 132, 0.32) 100%);
}}
.speed-wrap:hover input[type=range] {{
    height: 6px;
}}
.speed-val {{
    color: rgba(214, 217, 227, 0.95); background: rgba(8,10,18,0.68);
    width: 58px; height: 24px; padding: 2px 5px; text-align: center;
    font-family: Consolas, monospace; font-size: 10px; font-weight: 700;
    border: 1px solid rgba(110,116,140,0.42); border-radius: 6px; outline: none;
    appearance: textfield;
}}
.speed-val:focus {{ border-color: #ff8a65; box-shadow: 0 0 0 2px rgba(255,138,101,0.14); }}
.speed-val::-webkit-inner-spin-button {{ opacity: 1; }}
.delay-wrap {{
    display: flex; align-items: center; gap: 6px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(20,22,34,0.42);
    border: 1px solid rgba(102, 106, 126, 0.22);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.02), 0 1px 6px rgba(0,0,0,0.14);
    backdrop-filter: blur(2px);
    transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}}
.delay-wrap:hover {{
    background: rgba(14,14,24,0.64);
    border-color: rgba(92, 96, 116, 0.5);
    box-shadow: 0 3px 12px rgba(0,0,0,0.24);
}}
.delay-label {{
    color: rgba(214, 217, 227, 0.92);
    font-size: 10px;
    font-weight: 700;
    white-space: nowrap;
}}
.delay-wrap input[type=range] {{
    width: 64px; min-width: 64px;
    --delay-fill-start: 50%; --delay-fill-end: 50%;
    background: linear-gradient(to right,
        rgba(108, 112, 132, 0.32) 0%,
        rgba(108, 112, 132, 0.32) var(--delay-fill-start),
        #4dd0e1 var(--delay-fill-start),
        #4dd0e1 var(--delay-fill-end),
        rgba(108, 112, 132, 0.32) var(--delay-fill-end));
}}
.delay-wrap:hover input[type=range] {{
    height: 6px;
}}
.delay-val {{
    color: rgba(214, 217, 227, 0.95); background: rgba(8,10,18,0.68);
    width: 56px; height: 24px; padding: 2px 5px; text-align: center;
    font-family: Consolas, monospace; font-size: 10px; font-weight: 700;
    border: 1px solid rgba(110,116,140,0.42); border-radius: 6px; outline: none;
    appearance: textfield;
}}
.delay-val:focus {{ border-color: #4dd0e1; box-shadow: 0 0 0 2px rgba(77,208,225,0.14); }}
.delay-val::-webkit-inner-spin-button {{ opacity: 1; }}
.delay-unit {{
    color: rgba(214, 217, 227, 0.7);
    font-size: 10px; font-weight: 700; white-space: nowrap;
}}
.seek-wrap input[type=range] {{
    width: auto; min-width: 80px; flex: 1 1 auto;
    --seek-progress: 0%;
    background: linear-gradient(to right, #4fc3f7 0%, #4fc3f7 var(--seek-progress), rgba(108, 112, 132, 0.32) var(--seek-progress), rgba(108, 112, 132, 0.32) 100%);
    transition: height 0.15s ease;
}}
.seek-wrap:hover,
.seek-wrap.seeking {{
    background: rgba(14,14,24,0.64);
    border-color: rgba(92, 96, 116, 0.5);
    box-shadow: 0 3px 12px rgba(0,0,0,0.24);
}}
.seek-wrap:hover input[type=range],
.seek-wrap.seeking input[type=range] {{
    height: 6px;
}}
.seek-wrap:hover input[type=range]::-webkit-slider-thumb,
.seek-wrap.seeking input[type=range]::-webkit-slider-thumb {{
    transform: scale(1.08);
}}
.seek-wrap:hover input[type=range]::-moz-range-thumb,
.seek-wrap.seeking input[type=range]::-moz-range-thumb {{
    transform: scale(1.08);
}}
.seek-tip {{
    position: absolute; left: 0; bottom: calc(100% + 8px);
    padding: 3px 7px; border-radius: 6px;
    background: rgba(16,16,28,0.96); border: 1px solid #333;
    color: #f5f5f7; font-family: Consolas, monospace; font-size: 11px;
    white-space: nowrap; pointer-events: none; opacity: 0;
    transform: translateX(-50%);
    transition: opacity 0.12s ease;
}}
.seek-wrap:hover .seek-tip,
.seek-wrap.seeking .seek-tip {{
    opacity: 1;
}}
.time-val {{
    color: rgba(214, 217, 227, 0.9); min-width: 92px; flex: 0 0 92px; text-align: center;
    font-family: Consolas, monospace; font-size: 10px; font-weight: 700;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}}
@media (max-width: 1400px) {{
    .speed-delay-group {{ width: 100%; flex: 1 1 100%; gap: 6px; }}
    .speed-wrap,
    .delay-wrap {{ min-width: 0; flex: 1 1 0; padding-left: 6px; padding-right: 6px; }}
    .speed-wrap input[type=range],
    .delay-wrap input[type=range] {{ width: 36px; min-width: 36px; flex: 1 1 36px; }}
    .speed-val {{ width: 50px; }}
    .delay-val {{ width: 48px; }}
}}
@media (max-width: 700px), (max-height: 620px) {{
    :root {{ --play-position: 20%; }}
    .info-pane {{ padding: 18px; }}
    .song-meta {{ padding-bottom: 12px; }}
    .song-title {{ font-size: 18px; }}
    .song-difficulty {{ font-size: 11px; }}
    .video-crop {{ width: min(100%, calc(100vh - var(--rhythm-height))); }}
    .chart-details {{ margin-top: 8px; column-gap: 12px; }}
    .detail-item {{ padding: 5px 0; }}
    .bpm-readout {{ width: calc(var(--pentagon-width) - 10px); }}
    .bpm-readout span {{ font-size: 8px; }}
    .bpm-readout strong {{ margin-top: 4px; font-size: 26px; }}
    .controls {{ gap: 5px; padding: 7px 8px; }}
    .control-buttons {{ gap: 4px; }}
    .controls button {{ width: 24px; height: 24px; }}
    .measure-status {{ min-width: 72px; height: 26px; padding: 3px 8px; }}
    .measure-status {{ --status-number-size: 13px; }}
    .meter-status {{ min-width: 62px; height: 26px; padding: 3px 8px; }}
    .meter-status {{ --status-number-size: 13px; }}
    .seek-wrap {{ flex-basis: 190px; padding: 2px 6px; }}
    .seek-wrap input[type=range] {{ min-width: 70px; }}
    .speed-wrap {{ padding: 2px 6px; gap: 5px; }}
    .speed-wrap input[type=range] {{ width: 62px; min-width: 62px; }}
    .delay-wrap {{ padding: 2px 6px; gap: 5px; }}
    .delay-wrap input[type=range] {{ width: 62px; min-width: 62px; }}
    .time-val {{ min-width: 86px; flex-basis: 86px; font-size: 9px; }}
}}
</style>
</head>
<body data-renderer-version="{PLAYER_RENDERER_VERSION}">

<div class="local-file-warning" id="localFileWarning" hidden>
    当前通过本地文件打开，浏览器可能阻止视频预加载。建议关闭本页并双击同目录的“打开分析页面.cmd”。
</div>

<div class="video-area">
    <div class="video-pane">
        <div class="video-crop">
            <video id="pv" preload="auto">
{video_sources}
            </video>
        </div>
        <div class="video-empty" id="videoEmpty">
            <strong>预览视频不可用</strong>{html.escape(pv_name)}
        </div>
    </div>
    <div class="info-pane">
        <div class="song-meta">
            <div class="song-meta-body">
                <div class="song-title">{html.escape(song.title)}</div>
                <div class="song-difficulty">{html.escape(str(diff_name))} · Lv.{ch.level}</div>
            </div>
        </div>
        <div class="chart-details">
            <div class="detail-item"><span>艺术家</span><strong>{html.escape(song.artist or '—')}</strong></div>
            <div class="detail-item"><span>谱师</span><strong>{html.escape(ch.designer or '—')}</strong></div>
            <div class="detail-item"><span>BPM 范围</span><strong>{bpm_range}</strong></div>
            <div class="detail-item"><span>谱面时长</span><strong>{duration_text}</strong></div>
            <div class="detail-item"><span>分类</span><strong>{html.escape(song.genre or '—')}</strong></div>
            <div class="detail-item"><span>版本</span><strong>{html.escape(song.version or '—')}</strong></div>
        </div>
        <div class="controls">
            <div class="control-buttons">
                <button id="btnPlay" title="播放" aria-label="播放" disabled>&#9654;</button>
                <button id="btnRewind" title="回到开头" aria-label="回到开头" disabled>&#8634;</button>
            </div>
            <div class="measure-status" title="当前小节">
                <span>小节</span><strong id="measureNumber">1</strong><em>/ {len(measure_boundaries)}</em>
            </div>
            <div class="meter-status" title="当前拍号">
                <span>拍号</span><strong id="meterSignature">{html.escape(meter_sections[0]['signature'])}</strong>
            </div>
            <div class="seek-wrap">
                <input type="range" id="seekSlider" min="0" max="1" step="0.001" value="0" disabled>
                <span class="seek-tip" id="seekTip">0:00</span>
                <span class="time-val" id="timeVal">0:00 / 0:00</span>
            </div>
            <div class="speed-delay-group">
                <div class="speed-wrap">
                    <span class="speed-label">倍速</span>
                    <input type="range" id="speedSlider" min="0.25" max="2.00" step="0.01" value="1.00" disabled>
                    <input type="number" class="speed-val" id="speedInput" min="0.25" max="2.00" step="0.01" value="1.00" inputmode="decimal" aria-label="播放倍速" disabled>
                </div>
                <div class="delay-wrap">
                    <span class="delay-label">延迟</span>
                    <input type="range" id="delaySlider" min="-1000" max="1000" step="1" value="0" disabled>
                    <input type="number" class="delay-val" id="delayInput" min="-1000" max="1000" step="1" value="0" inputmode="numeric" aria-label="微调延迟 毫秒" disabled>
                    <span class="delay-unit">ms</span>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="rhythm-container">
    <div class="svg-container">
        <div class="scrolling-stage">
            <div class="virtual-strip" id="virtualStrip"
                 aria-hidden="true"></div>
            {scrolling_svg_html}
        </div>
    </div>
    <div class="left-pentagon" style="background-color: #282828; z-index: 2; left: 0;"></div>
    <div class="left-pentagon" style="background-color: #ffffff; z-index: 3; left: -{pentagon_white_shift}px;"></div>
    <div class="left-pentagon" style="background-color: #282828; z-index: 4; left: -{pentagon_dark_shift}px;"></div>
    <div class="bpm-readout">
        <span>BPM</span>
        <strong id="bpmNumber">{bpm:g}</strong>
    </div>
    <div class="play-marker" aria-hidden="true"></div>
</div>

<script type="text/plain" id="disabledStripVideoPlayer">
window.__RHYTHM_PLAYER_ERRORS__ = [];
window.addEventListener('error', (event) => {{
    window.__RHYTHM_PLAYER_ERRORS__.push({{
        message: event.message,
        line: event.lineno,
        column: event.colno,
    }});
}});
// ===== 参数 =====
const PX_PER_BEAT = {PX_PER_BEAT};
const PAD_X = {PAD_X};
const SVG_SCALE = {SVG_SCALE};
const BPM = {bpm};
const timings = {json.dumps(timings_js)};
const MEASURE_BOUNDARIES = {json.dumps(measure_boundaries)};
const METER_SECTIONS = {json.dumps(meter_sections, ensure_ascii=False)};
const VIDEO_OFFSET = {auto_offset};
const START_DISPLAY_BEAT = {start_display_beat};

// ===== DOM =====
const btnPlay = document.getElementById('btnPlay');
const btnRewind = document.getElementById('btnRewind');
const seekSlider = document.getElementById('seekSlider');
const seekTip = document.getElementById('seekTip');
const timeVal = document.getElementById('timeVal');
const speedSlider = document.getElementById('speedSlider');
const speedInput = document.getElementById('speedInput');
const delaySlider = document.getElementById('delaySlider');
const delayInput = document.getElementById('delayInput');
const pv = document.getElementById('pv');
const stripVideo = document.getElementById('stripVideo');
const stripVideoEmpty = document.getElementById('stripVideoEmpty');
const videoEmpty = document.getElementById('videoEmpty');
const localFileWarning = document.getElementById('localFileWarning');
const bpmNumber = document.getElementById('bpmNumber');
const measureNumber = document.getElementById('measureNumber');
const meterSignature = document.getElementById('meterSignature');
const seekWrap = document.querySelector('.seek-wrap');
let isPlaying = false;
let videoReady = false;
let pvReady = false;
let stripReady = false;
let isSeeking = false;
let delayMs = 0;
let basePlaybackRate = 1;
let syncLoopId = null;
let syncLoopUsesVideoFrames = false;
let stripPlayRequested = false;
let stripFrameCallbackId = null;
let latestStripFrameTime = null;
let lastVisualDrift = 0;
let lastRateCorrectionAt = 0;
let stripRateCorrection = 0;
let resumeAfterStripBuffer = false;
let resumeAfterSeek = false;
let lastBpm = null;
let lastMeasureNumber = null;
let lastMeterSignature = null;
let lastTimeText = null;
let lastSeekPercent = null;
let seekRectCache = null;
let timingIndex = 0;
let lastSeekUiTime = null;
let lastStatusUiUpdate = 0;
const STATUS_UI_INTERVAL_MS = 100;
const STRIP_RATE_UPDATE_INTERVAL_MS = 250;
const STRIP_SYNC_DEADBAND = 1 / 120;
const STRIP_SYNC_SETTLE_SECONDS = 2.5;
const STRIP_MAX_RATE_CORRECTION = 0.02;
const STRIP_RATE_SMOOTHING = 0.45;
if (window.location.protocol === 'file:') localFileWarning.hidden = false;

// ===== 视频时间 → beat + 当前 BPM =====
function findTimingSegment(chartT) {{
    if (chartT <= timings[0].time) {{
        timingIndex = 0;
        return timings[0];
    }}
    const next = timings[timingIndex + 1];
    if (chartT >= timings[timingIndex].time && (!next || chartT < next.time)) {{
        return timings[timingIndex];
    }}
    let lo = 0;
    let hi = timings.length - 1;
    while (lo <= hi) {{
        const mid = (lo + hi) >> 1;
        if (timings[mid].time <= chartT) lo = mid + 1;
        else hi = mid - 1;
    }}
    timingIndex = Math.max(0, hi);
    return timings[timingIndex];
}}

function videoTimeToState(videoT) {{
    const chartT = videoT - VIDEO_OFFSET - delayMs / 1000;
    if (chartT <= timings[0].time) {{
        return {{ beat: START_DISPLAY_BEAT, bpm: timings[0].bpm }};
    }}
    const segment = findTimingSegment(chartT);
    return {{
        beat: segment.beat + (chartT - segment.time) * segment.bpm / 60,
        bpm: segment.bpm,
    }};
}}

function findMeasureNumber(beat) {{
    let lo = 0;
    let hi = MEASURE_BOUNDARIES.length;
    while (lo < hi) {{
        const mid = (lo + hi) >> 1;
        if (MEASURE_BOUNDARIES[mid] <= beat + 1e-6) lo = mid + 1;
        else hi = mid;
    }}
    return Math.max(1, Math.min(MEASURE_BOUNDARIES.length, lo));
}}

function findMeterSignature(beat) {{
    let lo = 0;
    let hi = METER_SECTIONS.length;
    while (lo < hi) {{
        const mid = (lo + hi) >> 1;
        if (METER_SECTIONS[mid].start_beat <= beat + 1e-6) lo = mid + 1;
        else hi = mid;
    }}
    return METER_SECTIONS[Math.max(0, lo - 1)].signature;
}}

window.__RHYTHM_ANALYSIS__ = {{
    timings, MEASURE_BOUNDARIES, METER_SECTIONS,
    videoTimeToState, findMeasureNumber, findMeterSignature,
}};

function formatBpm(value) {{
    return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/[.]?0+$/, '');
}}

function formatClock(seconds) {{
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const totalSeconds = Math.floor(seconds);
    const minutes = Math.floor(totalSeconds / 60);
    const remain = totalSeconds % 60;
    return minutes + ':' + String(remain).padStart(2, '0');
}}

function updateSeekProgress(progress) {{
    const pct = Math.max(0, Math.min(1, progress));
    const percent = Math.round(pct * 1000) / 10 + '%';
    if (percent !== lastSeekPercent) {{
        seekSlider.style.setProperty('--seek-progress', percent);
        lastSeekPercent = percent;
    }}
}}

function updateSpeedUi(value) {{
    const clamped = Math.max(0.25, Math.min(2.0, value));
    const progress = (clamped - 0.25) / 1.75;
    speedSlider.style.setProperty('--speed-progress', progress * 100 + '%');
    speedSlider.value = clamped.toFixed(2);
    if (document.activeElement !== speedInput) speedInput.value = clamped.toFixed(2);
}}

function setVideoAvailable(available) {{
    videoReady = Boolean(available);
    btnPlay.disabled = !videoReady;
    btnRewind.disabled = !videoReady;
    seekSlider.disabled = !videoReady;
    speedSlider.disabled = !videoReady;
    speedInput.disabled = !videoReady;
    delaySlider.disabled = !videoReady;
    delayInput.disabled = !videoReady;
}}

function setPlaybackRate(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    const rate = Math.max(0.25, Math.min(2.0, Math.round(parsed * 100) / 100));
    basePlaybackRate = rate;
    pv.playbackRate = rate;
    stripVideo.playbackRate = rate;
    stripRateCorrection = 0;
    lastRateCorrectionAt = 0;
    updateSpeedUi(rate);
}}

function updateDelayUi(value) {{
    const clamped = Math.max(-1000, Math.min(1000, Math.round(value)));
    const pct = (clamped + 1000) / 2000 * 100;
    let fillStart, fillEnd;
    if (clamped >= 0) {{ fillStart = 50; fillEnd = pct; }}
    else {{ fillStart = pct; fillEnd = 50; }}
    delaySlider.style.setProperty('--delay-fill-start', fillStart + '%');
    delaySlider.style.setProperty('--delay-fill-end', fillEnd + '%');
    delaySlider.value = clamped;
    if (document.activeElement !== delayInput) delayInput.value = clamped;
}}

function setDelay(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    const clamped = Math.max(-1000, Math.min(1000, Math.round(parsed)));
    delayMs = clamped;
    updateDelayUi(clamped);
    syncStripToVideo(pv.currentTime, true);
    renderStatus(pv.currentTime, true);
}}

function updateSeekTip(progress) {{
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    const clamped = Math.max(0, Math.min(1, progress));
    seekTip.textContent = formatClock(clamped * duration);
    seekTip.style.left = clamped * 100 + '%';
}}

function syncSeekUi(current, force = false) {{
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    const progress = videoReady && duration > 0 ? current / duration : 0;
    const shouldUpdateProgress = force || isSeeking || lastSeekUiTime === null || Math.abs(current - lastSeekUiTime) >= 0.1;
    if (shouldUpdateProgress) {{
        if (!isSeeking) seekSlider.value = progress;
        updateSeekProgress(isSeeking ? parseFloat(seekSlider.value) || 0 : progress);
        lastSeekUiTime = current;
    }}
    const timeText = formatClock(current) + ' / ' + formatClock(duration);
    if (timeText !== lastTimeText) {{
        timeVal.textContent = timeText;
        lastTimeText = timeText;
    }}
}}

function updateStatusUi(videoT, state, force = false) {{
    if (state.bpm !== lastBpm) {{
        lastBpm = state.bpm;
        bpmNumber.textContent = formatBpm(state.bpm);
    }}
    const currentMeasure = findMeasureNumber(state.beat);
    if (currentMeasure !== lastMeasureNumber) {{
        lastMeasureNumber = currentMeasure;
        measureNumber.textContent = String(currentMeasure);
    }}
    const currentMeter = findMeterSignature(state.beat);
    if (currentMeter !== lastMeterSignature) {{
        lastMeterSignature = currentMeter;
        meterSignature.textContent = currentMeter;
    }}
    syncSeekUi(videoT, force);
}}

// ===== 播放控制（PV 主时钟 + 节奏条柔和同步） =====
function stripTimeForVideo(videoT) {{
    return Math.max(0, videoT - VIDEO_OFFSET - delayMs / 1000);
}}

function renderStatus(videoT, force = false) {{
    const safeTime = Number.isFinite(videoT) ? videoT : 0;
    updateStatusUi(safeTime, videoTimeToState(safeTime), force);
}}

function clampStripTime(value) {{
    const duration = Number.isFinite(stripVideo.duration) ? stripVideo.duration : value;
    return Math.max(0, Math.min(value, Math.max(0, duration - 0.001)));
}}

function requestStripPlay() {{
    if (!isPlaying || !stripReady || stripPlayRequested || !stripVideo.paused) return;
    stripPlayRequested = true;
    stripVideo.play().catch((error) => {{
        console.warn('strip video play:', error);
    }}).finally(() => {{
        stripPlayRequested = false;
    }});
}}

function resetStripRateController() {{
    stripRateCorrection = 0;
    lastRateCorrectionAt = 0;
    lastVisualDrift = 0;
    stripVideo.playbackRate = basePlaybackRate;
}}

function observeStripFrame(_now, metadata) {{
    if (Number.isFinite(metadata.mediaTime)) {{
        latestStripFrameTime = metadata.mediaTime;
    }}
    stripFrameCallbackId = stripVideo.requestVideoFrameCallback(observeStripFrame);
}}

function startStripFrameObserver() {{
    if (
        stripFrameCallbackId === null
        && 'requestVideoFrameCallback' in stripVideo
    ) {{
        stripFrameCallbackId = stripVideo.requestVideoFrameCallback(
            observeStripFrame,
        );
    }}
}}

function syncStripToVideo(videoT, force = false, timestamp = performance.now()) {{
    if (!stripReady || !Number.isFinite(videoT)) return;
    const rawStripTime = videoT - VIDEO_OFFSET - delayMs / 1000;
    const desired = clampStripTime(Math.max(0, rawStripTime));
    if (rawStripTime < 0) {{
        if (!stripVideo.paused) stripVideo.pause();
        resetStripRateController();
        if (
            force
            && Math.abs(stripVideo.currentTime - desired) > STRIP_SYNC_DEADBAND
        ) {{
            stripVideo.currentTime = desired;
        }}
        return;
    }}

    if (force) {{
        const drift = desired - stripVideo.currentTime;
        if (Math.abs(drift) > STRIP_SYNC_DEADBAND) {{
            stripVideo.currentTime = desired;
        }}
        latestStripFrameTime = null;
        resetStripRateController();
        return;
    }}

    if (isPlaying) requestStripPlay();
    if (
        stripVideo.paused
        || timestamp - lastRateCorrectionAt < STRIP_RATE_UPDATE_INTERVAL_MS
    ) {{
        return;
    }}

    // Use the frame that was actually presented, not merely the decoder clock.
    // This measures visible A/V phase error and avoids chasing currentTime noise.
    const presentedStripTime = Number.isFinite(latestStripFrameTime)
        ? latestStripFrameTime
        : stripVideo.currentTime;
    const drift = desired - presentedStripTime;
    lastVisualDrift = drift;
    let targetCorrection = 0;
    if (Math.abs(drift) > STRIP_SYNC_DEADBAND) {{
        targetCorrection = Math.max(
            -STRIP_MAX_RATE_CORRECTION,
            Math.min(
                STRIP_MAX_RATE_CORRECTION,
                drift / STRIP_SYNC_SETTLE_SECONDS
                / Math.max(0.25, basePlaybackRate),
            ),
        );
    }}
    stripRateCorrection += (
        targetCorrection - stripRateCorrection
    ) * STRIP_RATE_SMOOTHING;
    if (
        targetCorrection === 0
        && Math.abs(stripRateCorrection) < 0.00025
    ) {{
        stripRateCorrection = 0;
    }}
    const correctedRate = basePlaybackRate * (1 + stripRateCorrection);
    if (Math.abs(stripVideo.playbackRate - correctedRate) >= 0.00025) {{
        stripVideo.playbackRate = correctedRate;
    }}
    lastRateCorrectionAt = timestamp;
}}

function stopSyncLoop() {{
    if (syncLoopId !== null) {{
        if (
            syncLoopUsesVideoFrames
            && 'cancelVideoFrameCallback' in pv
        ) {{
            pv.cancelVideoFrameCallback(syncLoopId);
        }} else {{
            cancelAnimationFrame(syncLoopId);
        }}
    }}
    syncLoopId = null;
    syncLoopUsesVideoFrames = false;
}}

function scheduleSyncLoop() {{
    if (!isPlaying || syncLoopId !== null) return;
    if ('requestVideoFrameCallback' in pv) {{
        syncLoopUsesVideoFrames = true;
        syncLoopId = pv.requestVideoFrameCallback(syncLoop);
    }} else {{
        syncLoopUsesVideoFrames = false;
        syncLoopId = requestAnimationFrame(syncLoop);
    }}
}}

function syncLoop(timestamp, metadata = null) {{
    syncLoopId = null;
    if (!isPlaying) {{
        return;
    }}
    const videoT = (
        metadata && Number.isFinite(metadata.mediaTime)
    ) ? metadata.mediaTime : (
        Number.isFinite(pv.currentTime) ? pv.currentTime : 0
    );
    syncStripToVideo(videoT, false, timestamp);
    if (timestamp - lastStatusUiUpdate >= STATUS_UI_INTERVAL_MS) {{
        renderStatus(videoT);
        lastStatusUiUpdate = timestamp;
    }}
    scheduleSyncLoop();
}}

function startSyncLoop() {{
    scheduleSyncLoop();
}}

function play() {{
    if (isPlaying || !videoReady) return;
    isPlaying = true;
    btnPlay.textContent = 'Ⅱ';
    btnPlay.title = '暂停';
    btnPlay.setAttribute('aria-label', '暂停');
    pv.muted = false;
    syncStripToVideo(pv.currentTime, true);
    // Start the audible PV first. Different decoders have different startup
    // latency; launching both promises together lets the strip run ahead.
    // The `playing` handler starts the strip from the first active PV frame.
    pv.play().then(() => {{
        if (!isPlaying) return;
        startSyncLoop();
    }}).catch((error) => {{
        console.warn('preview video play:', error);
        pause();
    }});
}}

function pause() {{
    isPlaying = false;
    resumeAfterStripBuffer = false;
    resumeAfterSeek = false;
    btnPlay.textContent = '▶';
    btnPlay.title = '播放';
    btnPlay.setAttribute('aria-label', '播放');
    stopSyncLoop();
    pv.pause();
    stripVideo.pause();
    resetStripRateController();
    syncStripToVideo(pv.currentTime, true);
    renderStatus(pv.currentTime, true);
}}

function seekTo(time) {{
    if (!videoReady || !Number.isFinite(time)) return;
    const duration = Number.isFinite(pv.duration) ? pv.duration : time;
    const target = Math.max(0, Math.min(duration, time));
    pv.currentTime = target;
    syncStripToVideo(target, true);
    renderStatus(target, true);
}}

function rewind() {{
    pause();
    seekTo(0);
}}

function pauseForStripBuffering() {{
    if (!isPlaying || resumeAfterStripBuffer) return;
    resumeAfterStripBuffer = true;
    stopSyncLoop();
    pv.pause();
    stripVideo.pause();
    resetStripRateController();
}}

function resumeFromStripBuffering() {{
    if (!resumeAfterStripBuffer || !isPlaying || isSeeking) return;
    resumeAfterStripBuffer = false;
    syncStripToVideo(pv.currentTime, true);
    pv.play().then(() => {{
        if (isPlaying) startSyncLoop();
    }}).catch((error) => {{
        console.warn('resume after strip buffering:', error);
        pause();
    }});
}}

btnPlay.addEventListener('click', () => {{ if (isPlaying) pause(); else play(); }});
btnRewind.addEventListener('click', rewind);

// ===== 进度条 =====
seekSlider.addEventListener('input', (e) => {{
    if (!isSeeking) {{
        resumeAfterSeek = isPlaying;
        if (resumeAfterSeek) {{
            stopSyncLoop();
            pv.pause();
            stripVideo.pause();
            resetStripRateController();
        }}
    }}
    isSeeking = true;
    seekWrap.classList.add('seeking');
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    const progress = parseFloat(e.target.value) || 0;
    if (videoReady && duration > 0) seekTo(progress * duration);
    updateSeekTip(progress);
}});
seekSlider.addEventListener('pointermove', (e) => {{
    if (seekRectCache === null) seekRectCache = seekSlider.getBoundingClientRect();
    const progress = seekRectCache.width > 0 ? (e.clientX - seekRectCache.left) / seekRectCache.width : 0;
    if (!isSeeking) updateSeekTip(progress);
}});
seekSlider.addEventListener('pointerleave', () => {{ seekRectCache = null; }});
seekSlider.addEventListener('change', () => {{
    isSeeking = false;
    seekWrap.classList.remove('seeking');
    const shouldResume = resumeAfterSeek;
    resumeAfterSeek = false;
    if (resumeAfterStripBuffer) {{
        resumeFromStripBuffering();
    }} else if (shouldResume && isPlaying) {{
        syncStripToVideo(pv.currentTime, true);
        pv.play().then(() => {{
            if (isPlaying) startSyncLoop();
        }}).catch((error) => {{
            console.warn('resume after seek:', error);
            pause();
        }});
    }}
    renderStatus(pv.currentTime, true);
}});
seekSlider.addEventListener('pointerdown', () => seekWrap.classList.add('seeking'));
seekSlider.addEventListener('pointerup', () => {{
    if (!isSeeking) seekWrap.classList.remove('seeking');
}});

// ===== 速度 / 延迟 =====
speedSlider.addEventListener('input', (e) => setPlaybackRate(e.target.value));
speedInput.addEventListener('input', (e) => setPlaybackRate(e.target.value));
speedInput.addEventListener('change', () => {{
    setPlaybackRate(speedInput.value || 1);
    speedInput.value = basePlaybackRate.toFixed(2);
}});
delaySlider.addEventListener('input', (e) => setDelay(e.target.value));
delayInput.addEventListener('input', (e) => setDelay(e.target.value));
delayInput.addEventListener('change', () => {{
    setDelay(delayInput.value || 0);
    delayInput.value = delayMs;
}});

// ===== 键盘 =====
document.addEventListener('keydown', (e) => {{
    if (e.target instanceof HTMLInputElement) return;
    if (e.code === 'Space') {{
        e.preventDefault();
        if (isPlaying) pause(); else play();
    }} else if (e.code === 'ArrowLeft') {{
        e.preventDefault();
        seekTo(pv.currentTime - 1);
    }} else if (e.code === 'ArrowRight') {{
        e.preventDefault();
        seekTo(pv.currentTime + 1);
    }}
}});

function refreshAvailability() {{
    pv.hidden = !pvReady;
    videoEmpty.hidden = pvReady;
    stripVideoEmpty.hidden = stripReady;
    setVideoAvailable(pvReady && stripReady);
    if (pvReady) renderStatus(pv.currentTime, true);
}}

// ===== 视频事件 =====
pv.addEventListener('ended', () => pause());
pv.addEventListener('loadedmetadata', () => {{
    pvReady = true;
    seekSlider.value = 0;
    setPlaybackRate(parseFloat(speedSlider.value) || 1);
    refreshAvailability();
}});
pv.addEventListener('error', () => {{
    pvReady = false;
    refreshAvailability();
}});
pv.addEventListener('waiting', () => {{
    stripVideo.pause();
    stripVideo.playbackRate = basePlaybackRate;
}});
pv.addEventListener('playing', () => {{
    if (isPlaying) {{
        // The strip was already primed while both videos were paused.
        // Let the first presented PV frame drive its start; seeking again in
        // `playing` can use a decoder-ahead clock and make the strip lead.
        startSyncLoop();
    }}
}});
pv.addEventListener('seeking', () => stripVideo.pause());
pv.addEventListener('seeked', () => {{
    syncStripToVideo(pv.currentTime, true);
    // The first presented PV frame restarts the strip through syncLoop.
    // Starting it here would let the strip run during PV decoder warm-up.
    renderStatus(pv.currentTime, true);
}});
pv.addEventListener('timeupdate', () => {{
    if (!isPlaying) renderStatus(pv.currentTime, true);
}});

stripVideo.addEventListener('loadedmetadata', () => {{
    stripReady = true;
    stripVideo.playbackRate = basePlaybackRate;
    syncStripToVideo(pv.currentTime, true);
    startStripFrameObserver();
    refreshAvailability();
}});
stripVideo.addEventListener('error', () => {{
    stripReady = false;
    resumeAfterStripBuffer = false;
    refreshAvailability();
}});
stripVideo.addEventListener('waiting', pauseForStripBuffering);
stripVideo.addEventListener('stalled', pauseForStripBuffering);
stripVideo.addEventListener('canplay', resumeFromStripBuffering);

window.addEventListener('resize', () => {{
    seekRectCache = null;
}});

Object.assign(window.__RHYTHM_ANALYSIS__, {{
    stripTimeForVideo,
    syncStripToVideo,
    getSyncState: () => ({{
        videoTime: pv.currentTime,
        stripTime: stripVideo.currentTime,
        expectedStripTime: stripTimeForVideo(pv.currentTime),
        visualStripTime: latestStripFrameTime,
        visualDrift: lastVisualDrift,
        stripPlaybackRate: stripVideo.playbackRate,
        basePlaybackRate,
    }}),
}});

// ===== 初始状态 =====
updateSpeedUi(1);
updateDelayUi(0);
pvReady = pv.readyState >= 1;
stripReady = stripVideo.readyState >= 1 && Boolean(stripVideo.currentSrc);
setPlaybackRate(1);
if (stripReady) startStripFrameObserver();
refreshAvailability();
renderStatus(0, true);
</script>
<script>
// ===== 高性能分段 SVG 播放器 =====
const PX_PER_BEAT_SVG = {PX_PER_BEAT};
const PAD_X_SVG = {PAD_X};
const SVG_DISPLAY_SCALE = {SVG_SCALE};
const TIMINGS_SVG = {json.dumps(timings_js)};
const MEASURE_BOUNDARIES_SVG = {json.dumps(measure_boundaries)};
const METER_SECTIONS_SVG = {json.dumps(meter_sections, ensure_ascii=False)};
const VIDEO_OFFSET_SVG = {auto_offset};
const START_DISPLAY_BEAT_SVG = {start_display_beat};
const STRIP_WIDTH_SVG = {svg_w};
const SEGMENTS_SVG = {json.dumps(segments_js, ensure_ascii=False)};
const HAS_SEGMENTS_SVG = SEGMENTS_SVG.length > 0;
const STATUS_INTERVAL_SVG_MS = 100;

const virtualStripSvg = document.getElementById('virtualStrip');
const fullStripSvg = document.getElementById('svgScroll');
const playMarkerSvg = document.querySelector('.play-marker');
const btnPlaySvg = document.getElementById('btnPlay');
const btnRewindSvg = document.getElementById('btnRewind');
const seekSliderSvg = document.getElementById('seekSlider');
const seekTipSvg = document.getElementById('seekTip');
const timeValSvg = document.getElementById('timeVal');
const speedSliderSvg = document.getElementById('speedSlider');
const speedInputSvg = document.getElementById('speedInput');
const delaySliderSvg = document.getElementById('delaySlider');
const delayInputSvg = document.getElementById('delayInput');
const pvSvg = document.getElementById('pv');
const videoEmptySvg = document.getElementById('videoEmpty');
const localFileWarningSvg = document.getElementById('localFileWarning');
const bpmNumberSvg = document.getElementById('bpmNumber');
const measureNumberSvg = document.getElementById('measureNumber');
const meterSignatureSvg = document.getElementById('meterSignature');
const seekWrapSvg = document.querySelector('.seek-wrap');

let playingSvg = false;
let seekingSvg = false;
let pvReadySvg = false;
let segmentsReadySvg = !HAS_SEGMENTS_SVG;
let rafSvg = null;
let delaySvgMs = 0;
let markerCenterSvg = null;
let lastDistanceSvg = null;
let lastBpmSvg = null;
let lastMeasureSvg = null;
let lastMeterSvg = null;
let lastTimeTextSvg = null;
let lastSeekPercentSvg = null;
let lastSeekUiTimeSvg = null;
let seekRectSvg = null;
let timingIndexSvg = 0;
let lastStatusAtSvg = 0;
let clockMediaSvg = 0;
let clockPerfSvg = 0;
let clockActiveSvg = false;
let renderedFramesSvg = 0;
let lastFrameAtSvg = null;
let maxFrameGapSvg = 0;
let lastStatsPublishAtSvg = 0;

if (window.location.protocol === 'file:') {{
    localFileWarningSvg.hidden = false;
}}
document.body.dataset.rendererMode = 'segmented-svg';
document.body.dataset.svgSegmentCount = String(SEGMENTS_SVG.length);
document.body.dataset.svgSegmentsReady = String(segmentsReadySvg);
if (HAS_SEGMENTS_SVG) {{
    fullStripSvg.hidden = true;
}} else {{
    console.warn('分段 SVG 不完整，暂时使用完整 SVG。');
}}

function findTimingSvg(chartTime) {{
    if (chartTime <= TIMINGS_SVG[0].time) {{
        timingIndexSvg = 0;
        return TIMINGS_SVG[0];
    }}
    const next = TIMINGS_SVG[timingIndexSvg + 1];
    if (
        chartTime >= TIMINGS_SVG[timingIndexSvg].time
        && (!next || chartTime < next.time)
    ) {{
        return TIMINGS_SVG[timingIndexSvg];
    }}
    let lo = 0;
    let hi = TIMINGS_SVG.length - 1;
    while (lo <= hi) {{
        const mid = (lo + hi) >> 1;
        if (TIMINGS_SVG[mid].time <= chartTime) lo = mid + 1;
        else hi = mid - 1;
    }}
    timingIndexSvg = Math.max(0, hi);
    return TIMINGS_SVG[timingIndexSvg];
}}

function videoStateSvg(videoTime) {{
    const chartTime = videoTime - VIDEO_OFFSET_SVG - delaySvgMs / 1000;
    if (chartTime <= TIMINGS_SVG[0].time) {{
        return {{
            beat: START_DISPLAY_BEAT_SVG,
            bpm: TIMINGS_SVG[0].bpm,
        }};
    }}
    const timing = findTimingSvg(chartTime);
    return {{
        beat: timing.beat
            + (chartTime - timing.time) * timing.bpm / 60,
        bpm: timing.bpm,
    }};
}}

function findMeasureNumberSvg(beat) {{
    let lo = 0;
    let hi = MEASURE_BOUNDARIES_SVG.length;
    while (lo < hi) {{
        const mid = (lo + hi) >> 1;
        if (MEASURE_BOUNDARIES_SVG[mid] <= beat + 1e-6) lo = mid + 1;
        else hi = mid;
    }}
    return Math.max(1, Math.min(MEASURE_BOUNDARIES_SVG.length, lo));
}}

function findMeterSignatureSvg(beat) {{
    let lo = 0;
    let hi = METER_SECTIONS_SVG.length;
    while (lo < hi) {{
        const mid = (lo + hi) >> 1;
        if (METER_SECTIONS_SVG[mid].start_beat <= beat + 1e-6) lo = mid + 1;
        else hi = mid;
    }}
    return METER_SECTIONS_SVG[Math.max(0, lo - 1)].signature;
}}

function formatBpmSvg(value) {{
    return Number.isInteger(value)
        ? String(value)
        : value.toFixed(2).replace(/[.]?0+$/, '');
}}

function formatClockSvg(seconds) {{
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    return Math.floor(total / 60) + ':'
        + String(total % 60).padStart(2, '0');
}}

function anchorClockSvg(
    mediaTime = pvReadySvg ? pvSvg.currentTime : 0,
    active = playingSvg
        && !seekingSvg
        && !pvSvg.paused
        && !pvSvg.ended
) {{
    clockMediaSvg = Number.isFinite(mediaTime) ? mediaTime : 0;
    clockPerfSvg = performance.now();
    clockActiveSvg = active;
}}

function playbackTimeSvg(timestamp = performance.now()) {{
    const actual = pvReadySvg && Number.isFinite(pvSvg.currentTime)
        ? pvSvg.currentTime
        : 0;
    if (
        !clockActiveSvg
        || !playingSvg
        || seekingSvg
        || pvSvg.paused
        || pvSvg.seeking
        || pvSvg.readyState < 2
    ) {{
        return actual;
    }}
    const predicted = clockMediaSvg
        + (timestamp - clockPerfSvg) / 1000 * pvSvg.playbackRate;
    const duration = Number.isFinite(pvSvg.duration)
        ? pvSvg.duration
        : predicted;
    return Math.max(0, Math.min(duration, predicted));
}}

function renderStripSvg(videoTime) {{
    const state = videoStateSvg(videoTime);
    if (markerCenterSvg === null) {{
        const marker = playMarkerSvg.getBoundingClientRect();
        markerCenterSvg = marker.left + marker.width / 2;
    }}
    const distance = state.beat * PX_PER_BEAT_SVG
        + PAD_X_SVG
        - markerCenterSvg / SVG_DISPLAY_SCALE;
    if (
        lastDistanceSvg === null
        || Math.abs(distance - lastDistanceSvg) >= 0.001
    ) {{
        const transform = 'translate3d(' + (-distance) + 'px,0,0)';
        if (HAS_SEGMENTS_SVG) virtualStripSvg.style.transform = transform;
        else fullStripSvg.style.transform = transform;
        lastDistanceSvg = distance;
    }}
    return state;
}}

function updateSeekFillSvg(progress) {{
    const clamped = Math.max(0, Math.min(1, progress));
    const value = Math.round(clamped * 1000) / 10 + '%';
    if (value !== lastSeekPercentSvg) {{
        seekSliderSvg.style.setProperty('--seek-progress', value);
        lastSeekPercentSvg = value;
    }}
}}

function updateStatusSvg(videoTime, state, force = false) {{
    if (state.bpm !== lastBpmSvg) {{
        lastBpmSvg = state.bpm;
        bpmNumberSvg.textContent = formatBpmSvg(state.bpm);
    }}
    const measure = findMeasureNumberSvg(state.beat);
    if (measure !== lastMeasureSvg) {{
        lastMeasureSvg = measure;
        measureNumberSvg.textContent = String(measure);
    }}
    const meter = findMeterSignatureSvg(state.beat);
    if (meter !== lastMeterSvg) {{
        lastMeterSvg = meter;
        meterSignatureSvg.textContent = meter;
    }}
    const duration = Number.isFinite(pvSvg.duration) ? pvSvg.duration : 0;
    if (
        force
        || seekingSvg
        || lastSeekUiTimeSvg === null
        || Math.abs(videoTime - lastSeekUiTimeSvg) >= 0.1
    ) {{
        const progress = duration > 0 ? videoTime / duration : 0;
        if (!seekingSvg) seekSliderSvg.value = progress;
        updateSeekFillSvg(
            seekingSvg
                ? Number.parseFloat(seekSliderSvg.value) || 0
                : progress
        );
        lastSeekUiTimeSvg = videoTime;
    }}
    const timeText = formatClockSvg(videoTime)
        + ' / ' + formatClockSvg(duration);
    if (timeText !== lastTimeTextSvg) {{
        lastTimeTextSvg = timeText;
        timeValSvg.textContent = timeText;
    }}
}}

function renderAllSvg(force = false) {{
    const videoTime = playbackTimeSvg();
    const state = renderStripSvg(videoTime);
    updateStatusSvg(videoTime, state, force);
}}

function frameSvg(timestamp) {{
    rafSvg = null;
    if (!playingSvg) return;
    if (lastFrameAtSvg !== null) {{
        maxFrameGapSvg = Math.max(maxFrameGapSvg, timestamp - lastFrameAtSvg);
    }}
    lastFrameAtSvg = timestamp;
    renderedFramesSvg += 1;
    const videoTime = playbackTimeSvg(timestamp);
    if (timestamp - lastStatsPublishAtSvg >= 1000) {{
        document.body.dataset.svgFrames = String(renderedFramesSvg);
        document.body.dataset.svgMaxFrameGap = maxFrameGapSvg.toFixed(3);
        document.body.dataset.svgVideoTime = videoTime.toFixed(4);
        lastStatsPublishAtSvg = timestamp;
    }}
    const state = renderStripSvg(videoTime);
    if (timestamp - lastStatusAtSvg >= STATUS_INTERVAL_SVG_MS) {{
        updateStatusSvg(videoTime, state);
        lastStatusAtSvg = timestamp;
    }}
    rafSvg = requestAnimationFrame(frameSvg);
}}

function startFramesSvg() {{
    if (rafSvg === null) rafSvg = requestAnimationFrame(frameSvg);
}}

function stopFramesSvg() {{
    if (rafSvg !== null) cancelAnimationFrame(rafSvg);
    rafSvg = null;
}}

function refreshAvailabilitySvg() {{
    pvReadySvg = pvSvg.readyState >= 1;
    const ready = pvReadySvg && segmentsReadySvg;
    pvSvg.hidden = !pvReadySvg;
    videoEmptySvg.hidden = pvReadySvg;
    btnPlaySvg.disabled = !ready;
    btnRewindSvg.disabled = !ready;
    seekSliderSvg.disabled = !ready;
    speedSliderSvg.disabled = !ready;
    speedInputSvg.disabled = !ready;
    delaySliderSvg.disabled = !ready;
    delayInputSvg.disabled = !ready;
    if (pvReadySvg) renderAllSvg(true);
}}

function prepareSegmentsSvg() {{
    if (!HAS_SEGMENTS_SVG) {{
        segmentsReadySvg = true;
        refreshAvailabilitySvg();
        return Promise.resolve();
    }}
    const fragment = document.createDocumentFragment();
    const decodes = [];
    for (let index = 0; index < SEGMENTS_SVG.length; index++) {{
        const segment = SEGMENTS_SVG[index];
        const image = document.createElement('img');
        image.className = 'svg-segment';
        image.src = segment.src;
        image.width = segment.width;
        image.height = {svg_h};
        image.decoding = 'sync';
        image.loading = 'eager';
        image.fetchPriority = index < 4 ? 'high' : 'auto';
        image.draggable = false;
        image.alt = '';
        image.setAttribute('aria-hidden', 'true');
        image.style.left = segment.x + 'px';
        image.style.width = segment.width + 'px';
        fragment.appendChild(image);
        if (typeof image.decode === 'function') {{
            decodes.push(image.decode().catch(() => undefined));
        }}
    }}
    virtualStripSvg.replaceChildren(fragment);
    return Promise.all(decodes).then(() => {{
        // Give Chromium one paint to upload decoded segment textures.
        return new Promise((resolve) => requestAnimationFrame(() => {{
            requestAnimationFrame(resolve);
        }}));
    }}).then(() => {{
        segmentsReadySvg = true;
        document.body.dataset.svgSegmentsReady = 'true';
        refreshAvailabilitySvg();
    }});
}}

function playSvg() {{
    if (playingSvg || btnPlaySvg.disabled) return;
    playingSvg = true;
    btnPlaySvg.textContent = 'Ⅱ';
    btnPlaySvg.title = '暂停';
    btnPlaySvg.setAttribute('aria-label', '暂停');
    pvSvg.muted = false;
    anchorClockSvg(pvSvg.currentTime, false);
    startFramesSvg();
    pvSvg.play().catch((error) => {{
        console.warn('video play:', error);
        pauseSvg();
    }});
}}

function pauseSvg() {{
    playingSvg = false;
    btnPlaySvg.textContent = '▶';
    btnPlaySvg.title = '播放';
    btnPlaySvg.setAttribute('aria-label', '播放');
    pvSvg.pause();
    stopFramesSvg();
    anchorClockSvg(pvSvg.currentTime, false);
    renderAllSvg(true);
}}

function seekSvg(time) {{
    if (!pvReadySvg || !Number.isFinite(time)) return;
    const duration = Number.isFinite(pvSvg.duration) ? pvSvg.duration : time;
    pvSvg.currentTime = Math.max(0, Math.min(duration, time));
    anchorClockSvg(pvSvg.currentTime, false);
    renderAllSvg(true);
}}

function setRateSvg(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    const rate = Math.max(
        0.25,
        Math.min(2, Math.round(parsed * 100) / 100)
    );
    const current = playbackTimeSvg();
    pvSvg.playbackRate = rate;
    anchorClockSvg(current, clockActiveSvg);
    const progress = (rate - 0.25) / 1.75 * 100;
    speedSliderSvg.style.setProperty('--speed-progress', progress + '%');
    speedSliderSvg.value = rate.toFixed(2);
    if (document.activeElement !== speedInputSvg) {{
        speedInputSvg.value = rate.toFixed(2);
    }}
}}

function setDelaySvg(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    delaySvgMs = Math.max(-1000, Math.min(1000, Math.round(parsed)));
    const percent = (delaySvgMs + 1000) / 20;
    const start = delaySvgMs >= 0 ? 50 : percent;
    const end = delaySvgMs >= 0 ? percent : 50;
    delaySliderSvg.style.setProperty('--delay-fill-start', start + '%');
    delaySliderSvg.style.setProperty('--delay-fill-end', end + '%');
    delaySliderSvg.value = delaySvgMs;
    if (document.activeElement !== delayInputSvg) {{
        delayInputSvg.value = delaySvgMs;
    }}
    renderAllSvg(true);
}}

btnPlaySvg.addEventListener('click', () => {{
    if (playingSvg) pauseSvg();
    else playSvg();
}});
btnRewindSvg.addEventListener('click', () => {{
    pauseSvg();
    seekSvg(0);
}});

seekSliderSvg.addEventListener('input', (event) => {{
    seekingSvg = true;
    seekWrapSvg.classList.add('seeking');
    const duration = Number.isFinite(pvSvg.duration) ? pvSvg.duration : 0;
    const progress = Number.parseFloat(event.target.value) || 0;
    if (duration > 0) seekSvg(progress * duration);
    seekTipSvg.textContent = formatClockSvg(progress * duration);
    seekTipSvg.style.left = progress * 100 + '%';
}});
seekSliderSvg.addEventListener('change', () => {{
    seekingSvg = false;
    seekWrapSvg.classList.remove('seeking');
    anchorClockSvg();
    renderAllSvg(true);
}});
seekSliderSvg.addEventListener('pointermove', (event) => {{
    if (seekRectSvg === null) {{
        seekRectSvg = seekSliderSvg.getBoundingClientRect();
    }}
    const progress = seekRectSvg.width > 0
        ? (event.clientX - seekRectSvg.left) / seekRectSvg.width
        : 0;
    if (!seekingSvg) {{
        seekTipSvg.textContent = formatClockSvg(
            progress * (Number.isFinite(pvSvg.duration) ? pvSvg.duration : 0)
        );
        seekTipSvg.style.left = progress * 100 + '%';
    }}
}});
seekSliderSvg.addEventListener('pointerleave', () => {{
    seekRectSvg = null;
}});

speedSliderSvg.addEventListener('input', (event) => {{
    setRateSvg(event.target.value);
}});
speedInputSvg.addEventListener('input', (event) => {{
    setRateSvg(event.target.value);
}});
speedInputSvg.addEventListener('change', () => {{
    setRateSvg(speedInputSvg.value || 1);
}});
delaySliderSvg.addEventListener('input', (event) => {{
    setDelaySvg(event.target.value);
}});
delayInputSvg.addEventListener('input', (event) => {{
    setDelaySvg(event.target.value);
}});
delayInputSvg.addEventListener('change', () => {{
    setDelaySvg(delayInputSvg.value || 0);
}});

document.addEventListener('keydown', (event) => {{
    if (event.target instanceof HTMLInputElement) return;
    if (event.code === 'Space') {{
        event.preventDefault();
        if (playingSvg) pauseSvg();
        else playSvg();
    }} else if (event.code === 'ArrowLeft') {{
        event.preventDefault();
        seekSvg(pvSvg.currentTime - 1);
    }} else if (event.code === 'ArrowRight') {{
        event.preventDefault();
        seekSvg(pvSvg.currentTime + 1);
    }}
}});

pvSvg.addEventListener('loadedmetadata', () => {{
    seekSliderSvg.value = 0;
    refreshAvailabilitySvg();
    setRateSvg(Number.parseFloat(speedSliderSvg.value) || 1);
    anchorClockSvg(0, false);
    renderAllSvg(true);
}});
pvSvg.addEventListener('playing', () => {{
    anchorClockSvg(pvSvg.currentTime, true);
    if (playingSvg) startFramesSvg();
}});
pvSvg.addEventListener('waiting', () => {{
    anchorClockSvg(pvSvg.currentTime, false);
}});
pvSvg.addEventListener('seeking', () => {{
    anchorClockSvg(pvSvg.currentTime, false);
}});
pvSvg.addEventListener('seeked', () => {{
    anchorClockSvg();
    renderAllSvg(true);
}});
pvSvg.addEventListener('ended', pauseSvg);
pvSvg.addEventListener('error', refreshAvailabilitySvg);
pvSvg.addEventListener('timeupdate', () => {{
    if (!playingSvg) renderAllSvg(true);
}});

window.addEventListener('resize', () => {{
    markerCenterSvg = null;
    seekRectSvg = null;
    renderAllSvg(true);
}});

window.__RHYTHM_ANALYSIS__ = {{
    timings: TIMINGS_SVG,
    MEASURE_BOUNDARIES: MEASURE_BOUNDARIES_SVG,
    METER_SECTIONS: METER_SECTIONS_SVG,
    videoTimeToState: videoStateSvg,
    findMeasureNumber: findMeasureNumberSvg,
    findMeterSignature: findMeterSignatureSvg,
    getRenderStats: () => ({{
        renderedFrames: renderedFramesSvg,
        maxFrameGapMs: maxFrameGapSvg,
        segmentCount: SEGMENTS_SVG.length,
        segmentsReady: segmentsReadySvg,
        usingSegments: HAS_SEGMENTS_SVG,
        videoTime: playbackTimeSvg(),
        scrollDistance: lastDistanceSvg,
    }}),
}};

setRateSvg(1);
setDelaySvg(0);
refreshAvailabilitySvg();
prepareSegmentsSvg();
renderAllSvg(true);
</script>
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    write_analysis_launcher(html_dir)
    print(f'  ✓ {out_path}')
    return str(out_path)


def available_chart_difficulties(song_dir):
    maidata = os.path.join(song_dir, 'maidata.txt')
    if not os.path.exists(maidata):
        return []
    return default_target_difficulties(parse_maidata(maidata).charts)


def output_needs_regeneration(song_dir, difficulty, out_path, force=False):
    if force or not os.path.exists(out_path):
        return True
    try:
        with open(out_path, "r", encoding="utf-8") as generated:
            head = generated.read(4096)
        if f'data-renderer-version="{PLAYER_RENDERER_VERSION}"' not in head:
            return True
    except OSError:
        return True
    offset_paths = [
        offset_file_path(song_dir, difficulty),
        legacy_difficulty_path(song_dir, difficulty, '_offset.txt'),
    ]
    if any(os.path.exists(path) and os.path.getmtime(path) > os.path.getmtime(out_path)
           for path in offset_paths):
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description='maimai 节奏解析网页生成')
    ap.add_argument('-i', '--input', default=None, help='歌曲根目录')
    ap.add_argument('-d', '--dir', default=None, help='只处理指定曲目名')
    ap.add_argument('-diff', '--difficulty', type=int, default=None,
                    help='难度 ID；不指定则默认只处理 MASTER/Re:MASTER')
    ap.add_argument('-offset', '--offset', type=float, default=0.0, help='初始延迟 (秒)')
    ap.add_argument('-f', '--force', action='store_true', help='强制重新生成')
    args = ap.parse_args()

    base_dir = os.path.abspath(args.input) if args.input else str(PROJECT_ROOT)
    if not os.path.isdir(base_dir):
        print(f'错误: {base_dir} 不存在'); sys.exit(1)

    songs = find_song_dirs(base_dir, args.dir)
    if not songs:
        print(f'在 {base_dir} 下未找到含 maidata.txt 的目录'); return

    difficulty_label = (DIFFICULTY_NAMES.get(args.difficulty, args.difficulty)
                        if args.difficulty is not None else '默认 MASTER/Re:MASTER')
    print(f'发现 {len(songs)} 首歌曲, {difficulty_label}\n')
    failures = 0
    for sd, sid in songs:
        difficulties = ([args.difficulty] if args.difficulty is not None
                        else available_chart_difficulties(sd))
        if not difficulties:
            print(f'  [{sid}] 未发现可生成的谱面难度')
            failures += 1
            continue
        for difficulty in difficulties:
            out_path = analysis_html_path(sd, difficulty)
            if not output_needs_regeneration(sd, difficulty, out_path, args.force):
                print(f'  [{sid}] 已有 {out_path.relative_to(sd)}, 跳过 (-f 强制)')
                continue
            try:
                if generate_html(sd, sid, difficulty, args.offset) is None:
                    failures += 1
            except Exception as e:
                import traceback
                print(f'  [{sid}] {DIFFICULTY_NAMES.get(difficulty, difficulty)} ✗ {e}')
                traceback.print_exc()
                failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

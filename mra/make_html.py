#!/usr/bin/env python3
"""
maimai 节奏解析网页生成器
=============================
生成包含谱面预览视频 + Arcaea 风格滚动节奏条的 HTML 页面。

布局: 上方视频 / 下方 SVG 滚动条 (渐隐 mask + 五边形对齐 + 旋转 BPM)
同步: 视频 currentTime 驱动 SVG 横向滚动，offset 由 align_audio.py 生成

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
                        NOTE_AREA_H, LABEL_GAP, LABEL_AREA_H, NOTE_CY, NOTE_R,
                        NOTE_OUTER_DIAMETER, SEGMENT_BEATS)
from .difficulty import (DIFFICULTY_NAMES, analysis_html_path, default_target_difficulties,
                         difficulty_file_stem, find_preview_video, legacy_difficulty_path,
                         offset_file_path, preview_video_candidates, strip_svg_path)
from .song_library import PROJECT_ROOT, find_song_dirs

# Arcaea 常量 (与 4.py 完全一致)
# 降低滚动条整体缩放，等价于降低屏幕上“每拍经过的像素数”，从而减慢观感滚动速度。
SVG_SCALE = 1.8

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
    if diff_id not in song.charts:
        print(f'  [{song_id}] 无难度 {diff_name}'); return
    ch = song.charts[diff_id]
    if not ch.notes:
        return

    bpm = song.bpm
    bpm_values = [value for _, value in ch.bpm_timeline] or [bpm]
    bpm_min, bpm_max = min(bpm_values), max(bpm_values)
    bpm_range = (f'{bpm_min:g}' if math.isclose(bpm_min, bpm_max)
                 else f'{bpm_min:g} – {bpm_max:g}')
    chart_duration = max((note.time_sec + note.duration_sec for note in ch.notes), default=0.0)
    duration_text = f'{int(chart_duration // 60)}:{int(chart_duration % 60):02d}'
    total_beats = time_to_beat(chart_duration, ch.bpm_timeline)
    meter_map = load_meter_map(song_root, diff_id, total_beats)
    measure_boundaries = meter_map.boundaries(0.0, total_beats)
    if not measure_boundaries:
        measure_boundaries = [0.0]
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

    # SVG 尺寸
    with open(svg_path, 'r', encoding='utf-8') as f:
        svg_head = f.read(500)
    m_svg = re.search(r'width="(\d+)"', svg_head)
    svg_w = int(m_svg.group(1)) if m_svg else 30000
    svg_h = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H  # compact SVG 高度
    segment_width = SEGMENT_BEATS * PX_PER_BEAT
    segment_dir = svg_path.parent / 'segments'
    segment_re = re.compile(r'^strip_seg_(\d{3})\.svg$')
    if not segment_dir.is_dir():
        segment_dir = song_root
        segment_re = re.compile(rf'^{re.escape(file_stem)}_strip_seg_(\d{{3}})\.svg$')
    found_segments = []
    for name in os.listdir(segment_dir):
        m_segment = segment_re.match(name)
        if m_segment:
            found_segments.append((int(m_segment.group(1)), name))
    found_segments.sort()
    segment_names = []
    if found_segments:
        indexes = [index for index, _name in found_segments]
        if indexes == list(range(len(indexes))):
            segment_names = [name for _index, name in found_segments]
    segments_js = []
    if segment_names:
        for index, name in enumerate(segment_names):
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
:root {{ --rhythm-height: {rhythm_height}px; --play-position: 16.8%; --marker-size: {marker_size}px; --marker-top: {marker_top}px; --pentagon-width: {pentagon_width}px; }}
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
    width: 100%; height: 100%; position: absolute;
    mask-image: none;
    -webkit-mask-image: none;
    z-index: 1;
}}
.scrolling-stage {{
    position: absolute; top: 0; left: 0;
    transform-origin: top left;
    transform: scale({SVG_SCALE});
    will-change: transform; z-index: 1;
}}
.virtual-strip {{
    position: absolute; top: 0; left: 0;
    width: {svg_w}px; height: {svg_h}px;
    transform: translate3d(0, 0, 0);
    will-change: transform;
    contain: layout paint style;
    backface-visibility: hidden;
    z-index: 1;
}}
.svg-segment {{
    position: absolute; top: 0;
    height: {svg_h}px;
    pointer-events: none;
}}
.scrolling-svg {{
    position: absolute; top: 0; left: 0;
    width: {svg_w}px; height: {svg_h}px;
    transform: translate3d(0, 0, 0);
    will-change: transform;
    contain: layout paint style;
    backface-visibility: hidden;
    z-index: 1;
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
    color: #fff; font-family: Consolas, monospace; font-size: 14px;
    line-height: 1; font-variant-numeric: tabular-nums;
}}
.measure-status em {{
    color: rgba(191,195,211,0.68); font-family: Consolas, monospace;
    font-size: 9px; font-style: normal; font-variant-numeric: tabular-nums;
}}
.meter-status {{
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
    color: #fff; font-family: Consolas, monospace; font-size: 13px;
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
    .measure-status strong {{ font-size: 13px; }}
    .meter-status {{ min-width: 62px; height: 26px; padding: 3px 8px; }}
    .meter-status strong {{ font-size: 12px; }}
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
<body>

<div class="video-area">
    <div class="video-pane">
        <div class="video-crop">
            <video id="pv" preload="metadata">
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
            <div class="virtual-strip" id="virtualStrip" aria-hidden="true"></div>
            <object class="scrolling-svg" id="svgScroll" data="{svg_name}" type="image/svg+xml" width="{svg_w}" height="{svg_h}"></object>
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

<script>
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
const STRIP_WIDTH = {svg_w};
const SEGMENT_BEATS = {SEGMENT_BEATS};
const SEGMENT_WIDTH = {segment_width};
const SEGMENTS = {json.dumps(segments_js, ensure_ascii=False)};

// ===== DOM =====
const virtualStrip = document.getElementById('virtualStrip');
const svgScroll = document.getElementById('svgScroll');
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
const videoEmpty = document.getElementById('videoEmpty');
const bpmNumber = document.getElementById('bpmNumber');
const measureNumber = document.getElementById('measureNumber');
const meterSignature = document.getElementById('meterSignature');
const playMarker = document.querySelector('.play-marker');
const seekWrap = document.querySelector('.seek-wrap');
let isPlaying = false;
let videoReady = false;
let isSeeking = false;
let rafId = null;
let delayMs = 0;
let cachedPlayPositionPx = null;
let lastScrollDistance = null;
let lastBpm = null;
let lastMeasureNumber = null;
let lastMeterSignature = null;
let lastTimeText = null;
let lastSeekPercent = null;
let seekRectCache = null;
let resizeRafId = null;
let timingIndex = 0;
let lastSeekUiTime = null;
const USE_SEGMENTS = SEGMENTS.length > 0;
const segmentElements = new Map();
let lastVisibleSegmentKey = '';
if (USE_SEGMENTS) {{
    svgScroll.hidden = true;
}}

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
    return `${{minutes}}:${{String(remain).padStart(2, '0')}}`;
}}

function updateSeekProgress(progress) {{
    const pct = Math.max(0, Math.min(1, progress));
    const percent = `${{Math.round(pct * 1000) / 10}}%`;
    if (percent !== lastSeekPercent) {{
        seekSlider.style.setProperty('--seek-progress', percent);
        lastSeekPercent = percent;
    }}
}}

function updateSpeedUi(value) {{
    const clamped = Math.max(0.25, Math.min(2.0, value));
    const progress = (clamped - 0.25) / 1.75;
    speedSlider.style.setProperty('--speed-progress', `${{progress * 100}}%`);
    speedSlider.value = clamped.toFixed(2);
    if (document.activeElement !== speedInput) speedInput.value = clamped.toFixed(2);
}}

function setPlaybackRate(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    const rate = Math.max(0.25, Math.min(2.0, Math.round(parsed * 100) / 100));
    pv.playbackRate = rate;
    updateSpeedUi(rate);
}}

function updateDelayUi(value) {{
    const clamped = Math.max(-1000, Math.min(1000, Math.round(value)));
    const pct = (clamped + 1000) / 2000 * 100;
    let fillStart, fillEnd;
    if (clamped >= 0) {{ fillStart = 50; fillEnd = pct; }}
    else {{ fillStart = pct; fillEnd = 50; }}
    delaySlider.style.setProperty('--delay-fill-start', `${{fillStart}}%`);
    delaySlider.style.setProperty('--delay-fill-end', `${{fillEnd}}%`);
    delaySlider.value = clamped;
    if (document.activeElement !== delayInput) delayInput.value = clamped;
}}

function setDelay(value) {{
    const parsed = Number.parseFloat(value);
    if (!Number.isFinite(parsed)) return;
    const clamped = Math.max(-1000, Math.min(1000, Math.round(parsed)));
    delayMs = clamped;
    updateDelayUi(clamped);
    renderFrame(true);
}}

function updateSeekTip(progress) {{
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    const clamped = Math.max(0, Math.min(1, progress));
    seekTip.textContent = formatClock(clamped * duration);
    seekTip.style.left = `${{clamped * 100}}%`;
}}

function syncSeekUi(force = false) {{
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    const current = videoReady ? pv.currentTime : 0;
    const progress = videoReady && duration > 0 ? current / duration : 0;
    const shouldUpdateProgress = force || isSeeking || lastSeekUiTime === null || Math.abs(current - lastSeekUiTime) >= 0.1;
    if (shouldUpdateProgress) {{
        if (!isSeeking) seekSlider.value = progress;
        updateSeekProgress(isSeeking ? parseFloat(seekSlider.value) || 0 : progress);
        lastSeekUiTime = current;
    }}
    const timeText = `${{formatClock(videoReady ? pv.currentTime : 0)}} / ${{formatClock(duration)}}`;
    if (timeText !== lastTimeText) {{
        timeVal.textContent = timeText;
        lastTimeText = timeText;
    }}
}}

function updateVisibleSegments(scrollDistance) {{
    if (!USE_SEGMENTS) return;
    const viewportWidth = window.innerWidth / SVG_SCALE;
    const startX = Math.max(0, scrollDistance - viewportWidth * 0.6);
    const endX = Math.min(STRIP_WIDTH, scrollDistance + viewportWidth * 1.8);
    const firstIndex = Math.max(0, Math.floor(Math.max(0, startX - PAD_X) / SEGMENT_WIDTH) - 1);
    const lastIndex = Math.min(SEGMENTS.length - 1, Math.ceil(Math.max(0, endX - PAD_X) / SEGMENT_WIDTH) + 1);
    const visible = [];
    for (let index = firstIndex; index <= lastIndex; index++) {{
        if (SEGMENTS[index]) visible.push(index);
    }}
    const key = visible.join(',');
    if (key === lastVisibleSegmentKey) return;
    lastVisibleSegmentKey = key;
    const keep = new Set(visible);
    for (const [index, element] of segmentElements) {{
        if (!keep.has(index)) {{
            element.remove();
            segmentElements.delete(index);
        }}
    }}
    for (const index of visible) {{
        if (segmentElements.has(index)) continue;
        const segment = SEGMENTS[index];
        const object = document.createElement('object');
        object.className = 'svg-segment';
        object.type = 'image/svg+xml';
        object.data = segment.src;
        object.width = segment.width;
        object.height = {svg_h};
        object.style.left = `${{segment.x}}px`;
        object.style.width = `${{segment.width}}px`;
        object.setAttribute('aria-hidden', 'true');
        virtualStrip.appendChild(object);
        segmentElements.set(index, object);
    }}
}}

function renderFrame(forceUi = false) {{
    const videoT = videoReady ? pv.currentTime : 0;
    const state = videoTimeToState(videoT);
    if (cachedPlayPositionPx === null) {{
        const markerRect = playMarker.getBoundingClientRect();
        cachedPlayPositionPx = markerRect.left + markerRect.width / 2;
    }}
    const scrollDistance = state.beat * PX_PER_BEAT + PAD_X - cachedPlayPositionPx / SVG_SCALE;
    const displayScrollDistance = Math.round(scrollDistance * 100) / 100;
    if (lastScrollDistance === null || Math.abs(displayScrollDistance - lastScrollDistance) >= 0.01) {{
        if (USE_SEGMENTS) {{
            virtualStrip.style.transform = `translate3d(${{-displayScrollDistance}}px, 0, 0)`;
            updateVisibleSegments(displayScrollDistance);
        }} else {{
            svgScroll.style.transform = `translate3d(${{-displayScrollDistance}}px, 0, 0)`;
        }}
        lastScrollDistance = displayScrollDistance;
    }}
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
    syncSeekUi(forceUi);
}}

function setVideoAvailable(available) {{
    videoReady = available;
    pv.hidden = !available;
    videoEmpty.hidden = available;
    btnPlay.disabled = !available;
    btnRewind.disabled = !available;
    seekSlider.disabled = !available;
    speedSlider.disabled = !available;
    speedInput.disabled = !available;
    delaySlider.disabled = !available;
    delayInput.disabled = !available;
    if (!available) syncSeekUi(true);
}}

// ===== 滚动 =====
function updateScroll() {{
    if (!isPlaying) return;
    renderFrame();
    rafId = requestAnimationFrame(updateScroll);
}}

// ===== 播放控制 =====
function play() {{
    if (isPlaying || !videoReady) return;
    isPlaying = true;
    btnPlay.textContent = 'Ⅱ';
    btnPlay.title = '暂停';
    btnPlay.setAttribute('aria-label', '暂停');
    pv.muted = false;
    pv.play().catch(e => {{
        console.warn('video play:', e);
        pause();
    }});
    updateScroll();
}}

function pause() {{
    isPlaying = false;
    btnPlay.textContent = '▶';
    btnPlay.title = '播放';
    btnPlay.setAttribute('aria-label', '播放');
    pv.pause();
    if (rafId) cancelAnimationFrame(rafId);
}}

function rewind() {{
    pause();
    if (videoReady) pv.currentTime = 0;
    renderFrame(true);
}}

btnPlay.addEventListener('click', () => {{ if (isPlaying) pause(); else play(); }});
btnRewind.addEventListener('click', rewind);

seekSlider.addEventListener('input', (e) => {{
    isSeeking = true;
    seekWrap.classList.add('seeking');
    const duration = Number.isFinite(pv.duration) ? pv.duration : 0;
    if (videoReady && duration > 0) {{
        pv.currentTime = parseFloat(e.target.value) * duration;
    }}
    updateSeekTip(parseFloat(e.target.value) || 0);
    renderFrame(true);
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
    syncSeekUi(true);
}});
seekSlider.addEventListener('pointerdown', () => seekWrap.classList.add('seeking'));
seekSlider.addEventListener('pointerup', () => {{
    if (!isSeeking) seekWrap.classList.remove('seeking');
}});

speedSlider.addEventListener('input', (e) => {{
    setPlaybackRate(e.target.value);
}});
speedInput.addEventListener('input', (e) => setPlaybackRate(e.target.value));
speedInput.addEventListener('change', () => {{
    setPlaybackRate(speedInput.value || 1);
    speedInput.value = pv.playbackRate.toFixed(2);
}});
delaySlider.addEventListener('input', (e) => setDelay(e.target.value));
delayInput.addEventListener('input', (e) => setDelay(e.target.value));
delayInput.addEventListener('change', () => {{
    setDelay(delayInput.value || 0);
    delayInput.value = delayMs;
}});

// 键盘
document.addEventListener('keydown', (e) => {{
    if (e.target instanceof HTMLInputElement) return;
    if (e.code === 'Space') {{
        e.preventDefault();
        if (isPlaying) pause(); else play();
    }} else if (e.code === 'ArrowLeft') {{
        e.preventDefault();
        if (videoReady) {{
            pv.currentTime = Math.max(0, pv.currentTime - 1);
            renderFrame(true);
        }}
    }} else if (e.code === 'ArrowRight') {{
        e.preventDefault();
        if (videoReady) {{
            const duration = Number.isFinite(pv.duration) ? pv.duration : pv.currentTime + 1;
            pv.currentTime = Math.min(duration, pv.currentTime + 1);
            renderFrame(true);
        }}
    }}
}});

pv.addEventListener('ended', () => pause());
pv.addEventListener('timeupdate', () => {{
    if (!isPlaying) renderFrame(true);
}});
pv.addEventListener('loadedmetadata', () => {{
    setVideoAvailable(true);
    seekSlider.value = 0;
    pv.playbackRate = parseFloat(speedSlider.value) || 1;
    updateSpeedUi(pv.playbackRate);
    renderFrame(true);
}});
pv.addEventListener('error', () => setVideoAvailable(false));
window.addEventListener('resize', () => {{
    if (resizeRafId) cancelAnimationFrame(resizeRafId);
    resizeRafId = requestAnimationFrame(() => {{
        resizeRafId = null;
        cachedPlayPositionPx = null;
        seekRectCache = null;
        renderFrame(true);
    }});
}});

// 初始位置
updateSpeedUi(parseFloat(speedSlider.value) || 1);
updateDelayUi(0);
setVideoAvailable(pv.readyState >= 1);
renderFrame(true);
</script>
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
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

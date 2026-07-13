#!/usr/bin/env python3
"""
maimai 谱面节奏可视化 v2 — ArcaeaRhythmAnalysis 风格
=====================================================
核心: 节奏长条图 (rhythm strip)
  - 横轴 = 拍数 (48px/拍), 一条超长水平条带 (SVG, 浏览器可滚动)
  - 折叠版 PNG (每 64 拍一行) 便于直接查看
  - 音符圆点 颜色 = 音符时值 (note value, 即节奏细分)
      4=深蓝(四分) 8=红(八分) 16=蓝(十六分) 24=绿(六连) 32=青
      12=橙(三连八分) 6=绿(三连四分) 3=绿(三连二分) 5=紫 7=紫 ...
  - 圆点 形状/外环 = maimai 音符类型
      TAP=实心圆  BREAK=黄外环  EX=紫虚线外环
      HOLD=横条(持续)  SLIDE=横条+星  TOUCH=小红环  FIREWORK=金星
  - EACH (同时押) = 同一 x 上下堆叠
  - 拍/小节网格, 附点=菱形, 连音数写在圆内, 下方白底标注时值
受 ArcaeaRhythmAnalysis (RydIShihara) 3.py 启发。
"""

import sys, os, math, argparse, html
from collections import Counter
from fractions import Fraction
from pathlib import Path

import numpy as np
import warnings
warnings.filterwarnings('ignore')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle, Polygon
from .simai_parser import (parse_maidata, NoteType, Note, Chart, SongData,
                           time_to_beat)
from .difficulty import (DIFFICULTY_NAMES, default_target_difficulties,
                         difficulty_file_stem, legacy_difficulty_path,
                         rhythm_png_path, rhythm_svg_path, strip_segment_base_path,
                         strip_svg_path)
from .meter import MeterMap, analyze_chart_meter
from .song_library import PROJECT_ROOT, find_song_dirs

# ============ 全局样式 ============
# 尝试加载中文字体
try:
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
except Exception:
    pass

SVG_TEXT_FONT_STACK = 'SimHei,Microsoft YaHei,Noto Sans CJK SC,DejaVu Sans'
MPL_TEXT_FONT_FAMILY = 'SimHei'

# --- Arcaea 时值配色表 (note_value → 颜色) ---
# 颜色含义: 深蓝=4分, 红=8分, 蓝=16分, 青=32分, 绿=三连, 橙=6分...
NV_COLOR = {
    1: '#1f3aaa', 2: '#1f3aaa', 3: '#006800', 4: '#1f3aaa', 5: '#5F0438',
    6: '#006800', 7: '#492788', 8: '#d61818', 9: '#396E72', 10: '#8F2874',
    11: '#5F0438', 12: '#ff7700', 13: '#492788', 14: '#7C3ED3', 15: '#396E72',
    16: '#0080ff', 18: '#009D6F', 20: '#FD4294', 21: '#8F2874', 22: '#7C3ED3',
    24: '#33ff00', 28: '#ae70ff', 32: '#00ffff', 36: '#009D6F', 40: '#FD4294',
    48: '#bbff77', 56: '#ae70ff', 64: '#bbff77', 72: '#33ff00', 96: '#bbff77',
    192: '#bbff77',
}
NV_COLOR_DEFAULT = '#777777'


def nv_color(nv) -> str:
    """返回指定 note_value 的配色，不在表中的用灰色"""
    if nv is None:
        return NV_COLOR_DEFAULT
    try:
        key = int(nv)
    except Exception:
        try:
            key = int(round(float(nv)))
        except Exception:
            return NV_COLOR_DEFAULT
    return NV_COLOR.get(key, NV_COLOR_DEFAULT)


def tuplet_judge(m) -> int:
    """
    连音判断: 提取节拍分数的独本质数因子。
    例: 3分的质因子=3, 6分=3, 12分=3, 5分=5, 7分=7...
    2的幂次 (2,4,8,16,32...) → 1 (非连音)
    """
    try:
        m = float(m)
    except Exception:
        return 1
    n = 2 * m
    if abs(n - round(n)) < 1e-6:
        n = int(round(n))
        while n % 2 == 0 and n > 1:
            n //= 2
        return n
    return 1


# ============ 节奏事件 ============
# 将原始音符列表转换为带有时值标注的"节奏事件"列表。
# 每个节奏事件 = 一个时间点上的音符组 (5ms内的音符合并为同时押)。

# 允许的节拍分数分母 (用于拟合最接近的节拍型)
ALLOWED_DENOMS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 16, 20, 24, 28, 32, 48, 64, 96, 192]
RUN_INFERENCE_MAX_LEN = 12
RUN_INFERENCE_MIN_LEN = 3
RUN_INFERENCE_MAX_REL_ERROR = 0.035
RUN_INFERENCE_MAX_REL_SPREAD = 0.05
RUN_INFERENCE_MAX_PHASE_ERROR = 0.06
RUN_INFERENCE_TUPLET_MAX_REL_SPREAD = 0.18
RUN_INFERENCE_TUPLET_MAX_PHASE_ERROR = 0.09
RUN_INFERENCE_QUINTUPLET_MAX_REL_SPREAD = 0.26
RUN_INFERENCE_QUINTUPLET_MAX_PHASE_ERROR = 0.22
RUN_INFERENCE_MAX_NORMALIZED_BEATS = 1.0


def snap_fraction(decimal: float, allowed_denoms) -> Fraction:
    """
    将拍间距小数拟合到最接近的音乐分数。
    遍历预设分母集合，找误差最小的分数。
    """
    return best_fraction_fit(decimal, allowed_denoms)[0]


def best_fraction_fit(decimal: float, allowed_denoms) -> tuple[Fraction, float, float]:
    """
    返回最接近的节拍分数拟合结果:
      (分数, 绝对误差, 相对误差)
    """
    best, bestd = None, float('inf')
    for d in allowed_denoms:
        num = int(round(decimal * d))
        if num <= 0:
            continue
        cand = Fraction(num, d)
        diff = abs(decimal - float(cand))
        if diff < bestd:
            bestd, best = diff, cand
    best = best or Fraction(1)
    rel = bestd / max(abs(float(best)), 1e-9)
    return best, bestd, rel


def _fraction_to_nv_label(frac: Fraction):
    """节拍分数 → (note_value, 标注文本)。"""
    four = Fraction(4) / frac
    six = Fraction(6) / frac
    if four.denominator == 1:
        return int(four), str(int(four))
    if six.denominator == 1 and frac < 4:
        return int(six), str(int(six)) + '.'
    return four, f"{four.numerator}/{four.denominator}"


def _delta_to_nv(delta, frac: Fraction | None = None):
    """
    拍间距 → (note_value, 标注文本)。
    note_value 用于配色 (4=蓝色四分, 8=红色八分, ...)
    标注文本: 整数=标准分拍, "N."=附点, "N/M"=复杂分拍
    """
    if delta <= 1e-6:
        return None, '-'
    frac = frac or snap_fraction(delta, ALLOWED_DENOMS)
    return _fraction_to_nv_label(frac)


def _can_normalize_as_tuplet(frac: Fraction, candidate_nv, tuplet_factor: int) -> bool:
    """
    Return whether a run is allowed to be normalized as a tuplet approximation.

    Normalization is only for short, fine-grid approximations such as 20/28 notes
    that were authored with many commas on 48th/96th-or-finer grids. Coarse
    rhythms with skipped commas (for example 3/2 or 2 beats in a {8} section)
    should keep their literal spacing and label.
    """
    if tuplet_factor <= 1:
        return False
    if not isinstance(candidate_nv, int):
        return False
    return float(frac) <= RUN_INFERENCE_MAX_NORMALIZED_BEATS


def _window_has_only_clean_direct_meter(window: list[float]) -> bool:
    """
    Return whether every interval in a window already has a clear direct label.

    A run like 16/12/12/12/16 is intentional coarse meter, not a hidden
    quintuplet just because its average is close to 20. Tuplet normalization is
    reserved for windows containing awkward direct labels such as 96/5 or
    384/19 that usually come from fine-grid comma approximation.
    """
    if not window:
        return False
    for delta in window:
        frac, _, rel_error = best_fraction_fit(delta, ALLOWED_DENOMS)
        if rel_error > RUN_INFERENCE_MAX_REL_ERROR:
            return False
        _, label = _fraction_to_nv_label(frac)
        if '/' in label:
            return False
    return True


def _tuplet_window_candidate(window: list[float]):
    if not window:
        return None
    mean_delta = sum(window) / len(window)
    frac, _, rel_error = best_fraction_fit(mean_delta, ALLOWED_DENOMS)
    target = float(frac)
    if target <= 1e-9:
        return None
    candidate_nv, _ = _fraction_to_nv_label(frac)
    tuplet_factor = tuplet_judge(candidate_nv) if candidate_nv not in (None, '-') else 1
    if not _can_normalize_as_tuplet(frac, candidate_nv, tuplet_factor):
        return None
    if _window_has_only_clean_direct_meter(window):
        return None
    rel_spread = max(abs(delta - target) / target for delta in window)
    cumulative = 0.0
    phase_errors = []
    for step_index, delta in enumerate(window, start=1):
        cumulative += delta
        expected = step_index * target
        phase_errors.append(abs(cumulative - expected) / target)
    max_phase_error = max(phase_errors) if phase_errors else 0.0
    if rel_error > RUN_INFERENCE_MAX_REL_ERROR:
        return None
    max_rel_spread = (RUN_INFERENCE_QUINTUPLET_MAX_REL_SPREAD
                      if tuplet_factor == 5 else RUN_INFERENCE_TUPLET_MAX_REL_SPREAD)
    max_phase_error_allowed = (RUN_INFERENCE_QUINTUPLET_MAX_PHASE_ERROR
                               if tuplet_factor == 5 else RUN_INFERENCE_TUPLET_MAX_PHASE_ERROR)
    if rel_spread > max_rel_spread:
        return None
    if max_phase_error > max_phase_error_allowed:
        return None
    return frac, rel_error, rel_spread, max_phase_error


def _infer_delta_fractions(deltas: list[float]) -> list[Fraction]:
    """
    针对连续的拍间距做两段式识别：
    1. 先对每个间距单独拟合
    2. 再对近似等距的连续区段做整体归一化

    这样即使底层谱面用了很细的逗号步长来近似 5 连 / 7 连等时值，
    只要一整段整体接近某种等分，也能被统一识别出来。
    """
    if not deltas:
        return []

    direct = [best_fraction_fit(delta, ALLOWED_DENOMS)[0] for delta in deltas]
    chosen = direct[:]
    n = len(deltas)
    idx = 0
    while idx < n:
        best_run = None
        max_len = min(RUN_INFERENCE_MAX_LEN, n - idx)
        for length in range(max_len, RUN_INFERENCE_MIN_LEN - 1, -1):
            window = deltas[idx:idx + length]
            mean_delta = sum(window) / length
            frac, _, rel_error = best_fraction_fit(mean_delta, ALLOWED_DENOMS)
            target = float(frac)
            if target <= 1e-9:
                continue
            candidate_nv, candidate_label = _fraction_to_nv_label(frac)
            tuplet_factor = tuplet_judge(candidate_nv) if candidate_nv not in (None, '-') else 1
            likely_tuplet = _can_normalize_as_tuplet(frac, candidate_nv, tuplet_factor)
            if tuplet_factor > 1 and not likely_tuplet:
                continue
            if likely_tuplet and _window_has_only_clean_direct_meter(window):
                continue
            rel_spread = max(abs(delta - target) / target for delta in window)
            cumulative = 0.0
            phase_errors = []
            for step_index, delta in enumerate(window, start=1):
                cumulative += delta
                expected = step_index * target
                phase_errors.append(abs(cumulative - expected) / target)
            max_phase_error = max(phase_errors) if phase_errors else 0.0
            if rel_error > RUN_INFERENCE_MAX_REL_ERROR:
                continue
            if tuplet_factor == 5:
                max_rel_spread = RUN_INFERENCE_QUINTUPLET_MAX_REL_SPREAD
                max_phase_error_allowed = RUN_INFERENCE_QUINTUPLET_MAX_PHASE_ERROR
            elif likely_tuplet:
                max_rel_spread = RUN_INFERENCE_TUPLET_MAX_REL_SPREAD
                max_phase_error_allowed = RUN_INFERENCE_TUPLET_MAX_PHASE_ERROR
            else:
                max_rel_spread = RUN_INFERENCE_MAX_REL_SPREAD
                max_phase_error_allowed = RUN_INFERENCE_MAX_PHASE_ERROR
            if rel_spread > max_rel_spread:
                continue
            if max_phase_error > max_phase_error_allowed:
                continue

            direct_labels = [_fraction_to_nv_label(direct[pos])[1] for pos in range(idx, idx + length)]
            direct_uniform = all(direct[pos] == frac for pos in range(idx, idx + length))
            if likely_tuplet:
                should_promote = True
            elif '/' in candidate_label:
                should_promote = False
            else:
                should_promote = (not direct_uniform) or any('/' in label for label in direct_labels)
            if not should_promote:
                continue

            score = (
                length,
                1 if likely_tuplet else 0,
                -max_phase_error,
                -rel_spread,
                -rel_error,
                1 if '/' not in candidate_label else 0,
            )
            if best_run is None or score > best_run[0]:
                best_run = (score, length, frac)

        if best_run is None:
            idx += 1
            continue

        _, length, frac = best_run
        for pos in range(idx, idx + length):
            chosen[pos] = frac
        idx += length

    # Tuplet approximations are often written on very fine grids with overlapping
    # 5-5-4-5-5 / similar comma patterns. A greedy run can recognize the first
    # half and leave the tail as 96/5 or 24, so do an overlapping tuplet-only
    # pass and let strong tuplet windows cover their full span.
    tuplet_votes: list[tuple[tuple, Fraction] | None] = [None] * n
    for start in range(n):
        max_len = min(RUN_INFERENCE_MAX_LEN, n - start)
        for length in range(RUN_INFERENCE_MIN_LEN, max_len + 1):
            candidate = _tuplet_window_candidate(deltas[start:start + length])
            if candidate is None:
                continue
            frac, rel_error, rel_spread, max_phase_error = candidate
            score = (length, -max_phase_error, -rel_spread, -rel_error)
            for pos in range(start, start + length):
                if tuplet_votes[pos] is None or score > tuplet_votes[pos][0]:
                    tuplet_votes[pos] = (score, frac)
    for pos, vote in enumerate(tuplet_votes):
        if vote is not None:
            chosen[pos] = vote[1]
    return chosen


def compute_rhythm_events(chart: Chart):
    """
    将 Chart 的原始音符列表转换为节奏事件列表。
    每个事件 = (时间, 音符组)，5ms 内的音符视为同时押合并在同一组。
    每组标注:
      nv_label  = "到下一个事件的节拍间隔" 的文本表示 (标注用)
      nv        = "到最近事件(前/后)的节拍间隔" 的 note_value (配色用)
    """
    notes = chart.notes
    if not notes:
        return []
    tl = chart.bpm_timeline
    # 按时间排序，5ms 内的 → 同一组(同时押)
    sn = sorted(notes, key=lambda n: n.time_sec)
    events = []  # [(time_sec, [Note, ...]), ...]
    cur_t, cur_g = sn[0].time_sec, [sn[0]]
    for n in sn[1:]:
        if n.time_sec - cur_t <= 0.005:
            cur_g.append(n)
        else:
            events.append((cur_t, cur_g))
            cur_t, cur_g = n.time_sec, [n]
    events.append((cur_t, cur_g))

    # 转换所有事件时间为拍数
    result = []
    beats = [time_to_beat(t, tl) for t, _ in events]
    deltas = [beats[i + 1] - beats[i] for i in range(len(beats) - 1)]
    delta_fracs = _infer_delta_fractions(deltas)
    display_beats = [beats[0]]
    for frac in delta_fracs:
        display_beats.append(display_beats[-1] + float(frac))
    for idx, (t, grp) in enumerate(events):
        b = beats[idx]
        display_b = display_beats[idx]
        # 标注用: 到下一个音符的时值
        nv_label = '-'
        if idx + 1 < len(events):
            delta_next = deltas[idx]
            _, nv_label = _delta_to_nv(delta_next, delta_fracs[idx])
        # 配色用: 到最近音符的时值 (min(前方间距, 后方间距))
        delta_prev = b - beats[idx - 1] if idx > 0 else float('inf')
        delta_next = beats[idx + 1] - b if idx + 1 < len(events) else float('inf')
        if idx == 0 and deltas:
            nearest = delta_next
            nearest_frac = delta_fracs[0]
        elif idx == len(events) - 1 and deltas:
            nearest = delta_prev
            nearest_frac = delta_fracs[-1]
        elif delta_prev <= delta_next:
            nearest = delta_prev
            nearest_frac = delta_fracs[idx - 1]
        else:
            nearest = delta_next
            nearest_frac = delta_fracs[idx]
        nv_for_style, style_label = _delta_to_nv(nearest, nearest_frac if deltas else None)
        result.append({'time': t, 'beat': b, 'display_beat': display_b, 'notes': grp,
                       'nv': nv_for_style, 'nv_label': nv_label,
                       'style_label': style_label})
    return result


# ============ 几何常量 ============
# 以下常量定义节奏长条图的布局尺寸（像素单位）。
# 采用"每拍固定像素"映射，变速不变距。
PX_PER_BEAT = 108         # 每拍像素宽度 (16分间距=27px，与节奏点外径相切)
PAD_X = 320               # 左侧额外预留空带，避免前端初始对齐时露出容器黑底
SEGMENT_BEATS = 64        # 前端 SVG 虚拟化分段长度；每段 16 小节，兼顾清晰度与 DOM 开销
NOTE_R = 11.5             # 节奏点填充半径；外环与填充之间保留 1px 黑色间隔
NOTE_RING_GAP = 1.0       # 节奏点填充与外环之间的黑色间隔
NOTE_RING_W = 1.0         # 节奏点外环宽度（不影响终点空心圆）
NOTE_RING_R = NOTE_R + NOTE_RING_GAP + NOTE_RING_W / 2
NOTE_OUTER_DIAMETER = 2 * (NOTE_RING_R + NOTE_RING_W / 2)
NOTE_RING_DASH = '3 2'    # 保护套 TAP / TOUCH 的虚线外环
BREAK_RING_COLOR = '#ff5a36'
NOTE_AREA_H = 46          # 黑色音符区加高，让小节线在节奏点后仍有可见长度
LABEL_AREA_H = 14         # 标注区高度 (白色底色, 显示时值文本)
ROW_GAP = 12              # 行间距
LABEL_GAP = 0             # 音符区与标注区之间的紧凑间隔
ROW_H = NOTE_AREA_H + LABEL_AREA_H + ROW_GAP  # 单行总高度
NOTE_CY = NOTE_AREA_H / 2                     # 音符区中心 Y
LABEL_CY = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H / 2  # 标注区中心 Y
ROW_BEATS_DEFAULT = 32    # 默认每行拍数 (8小节=32拍，便于折叠查看)
BEATS_PER_MEASURE = 4     # 兼容常量；实际小节长度由 MeterMap 决定
LONG_IMAGE_EXTRA_MEASURES = 12  # 长条图额外留白的小节数

# 音符合并优先级 (多押时取最重要的作为代表)
TYPE_PRIORITY = {
    NoteType.FIREWORK: 7, NoteType.BREAK: 6, NoteType.EX: 5,
    NoteType.SLIDE: 4, NoteType.HOLD: 3, NoteType.TAP: 2,
    NoteType.TOUCH_HOLD: 1, NoteType.TOUCH: 0,
}


def row_width_px(row_beats: int) -> int:
    return PAD_X * 2 + row_beats * PX_PER_BEAT


def beat_to_x_in_row(beat_in_row: float) -> float:
    return PAD_X + beat_in_row * PX_PER_BEAT


def star_path(cx, cy, r):
    pts = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        rr = r if k % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


def diamond_path(cx, cy, r):
    return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]


def _note_has_protected_judgement(note):
    return (
        note.is_ex
        or note.note_type in (NoteType.EX, NoteType.TOUCH, NoteType.TOUCH_HOLD)
    )


def _note_requires_press(note):
    """Return whether this note is a simultaneously pressed input for chord coloring."""
    return note.note_type != NoteType.SLIDE


def _event_ring_style(notes):
    has_break = any(note.is_break for note in notes)
    pressable_notes = [note for note in notes if _note_requires_press(note)]
    all_protected = bool(pressable_notes) and all(
        _note_has_protected_judgement(note) for note in pressable_notes
    )
    color = BREAK_RING_COLOR if has_break else '#ffffff'
    dash = NOTE_RING_DASH if all_protected else ''
    return color, dash


# ============ 原语构造 (SVG 与 matplotlib 共用) ============
# 原语: (kind, *args)

def build_primitives(events, row_beats, total_beats, bpm, chart, meter_map=None):
    """生成原语列表. 每行 row_beats 拍, 自动折叠. 多押合并为单个圆点.
    左右各留 PAD_X 留白, 避免边缘音符被截断."""
    n_rows = max(1, int(math.ceil(total_beats / row_beats)))
    prims = []
    meter_map = meter_map or MeterMap(default="4/4")
    W_row = row_width_px(row_beats)
    right_edge = PAD_X + row_beats * PX_PER_BEAT  # 网格右边界
    label_sizes = _fit_label_font_sizes(events)

    for row in range(n_rows):
        y0 = row * ROW_H
        b0 = row * row_beats
        # 黑/白底 (整行宽, 含留白). 标注区从 NOTE_AREA_H + LABEL_GAP 起
        prims.append(('rect', 0, y0, W_row, NOTE_AREA_H, '#0a0a14', 1.0))
        # 间隔区 (与音符区同色, 避免音符与标注挤在一起)
        prims.append(('rect', 0, y0 + NOTE_AREA_H, W_row, LABEL_GAP, '#0a0a14', 1.0))
        prims.append(('rect', 0, y0 + NOTE_AREA_H + LABEL_GAP, W_row, LABEL_AREA_H, '#ffffff', 1.0))
        # 左侧预留区也补齐网格，让前端初始状态显示完整“无键带”而不是空黑底。
        left_grid_beats = int(math.ceil(PAD_X / PX_PER_BEAT))
        grid_start = b0 - left_grid_beats
        grid_end = min(total_beats, b0 + row_beats)
        boundaries = meter_map.boundaries(grid_start, b0 + row_beats)
        boundary_keys = {round(boundary, 6) for boundary in boundaries}
        integer_beats = list(range(math.ceil(grid_start), math.floor(grid_end) + 1))
        grid_beats = sorted(set(float(beat) for beat in integer_beats) | set(boundaries))
        for absolute_beat in grid_beats:
            beat_in_row = absolute_beat - b0
            x = beat_to_x_in_row(beat_in_row)
            if not 0 <= x <= right_edge + 1e-6:
                continue
            is_measure = round(absolute_beat, 6) in boundary_keys
            if is_measure:
                prims.append(('line', x, y0 + 1, x, y0 + NOTE_AREA_H - 1,
                              '#ffffff', 2.0))
            elif abs(absolute_beat - round(absolute_beat)) < 1e-6:
                prims.append(('line', x, y0 + 3, x, y0 + 15, '#ffffff', 0.85))
                prims.append(('line', x, y0 + NOTE_AREA_H - 15,
                              x, y0 + NOTE_AREA_H - 3, '#ffffff', 0.85))

        # 每个四分音符拍内的四等分点；若该位置本身是变拍号小节线则不画点。
        for absolute_beat in range(math.ceil(grid_start), math.floor(grid_end) + 1):
            for sub in (0.25, 0.5, 0.75):
                sub_beat = absolute_beat + sub
                sx = beat_to_x_in_row(sub_beat - b0)
                if (sub_beat <= grid_end + 1e-6 and 0 <= sx < right_edge and
                        round(sub_beat, 6) not in boundary_keys):
                    prims.append(('dot', sx, y0 + NOTE_CY, 0.8, '#666666'))

        # 只在开头和拍号变化处标注，避免每小节重复文字。
        visible_measures = [measure for measure in meter_map.measures
                            if grid_start - 1e-6 <= measure.start_beat <= grid_end + 1e-6]
        for measure in visible_measures:
            index = meter_map.measures.index(measure)
            changed = (index == 0 or
                       meter_map.measures[index - 1].signature != measure.signature)
            if changed:
                x = beat_to_x_in_row(measure.start_beat - b0)
                if 0 <= x <= right_edge:
                    prims.append(('text', x + 3, y0 + 7, measure.signature.label,
                                  '#8bd5ff', 5.2, 'bold', 'normal', 'start'))
        # 行分隔线
        if row < n_rows - 1:
            prims.append(('line', 0, y0 + ROW_H - ROW_GAP / 2,
                          W_row, y0 + ROW_H - ROW_GAP / 2, '#333344', 0.6))

    # BPM 变化点。横轴按拍数绘制，因此变速不会改变谱面间距；标记用于说明实时速度变化。
    for change_time, change_bpm in chart.bpm_timeline[1:]:
        change_beat = time_to_beat(change_time, chart.bpm_timeline)
        if change_beat < 0 or change_beat > total_beats:
            continue
        row = min(int(change_beat // row_beats), n_rows - 1)
        beat_in_row = change_beat - row * row_beats
        x = beat_to_x_in_row(beat_in_row)
        y0 = row * ROW_H
        bpm_text = f'BPM {change_bpm:g}'
        prims.append(('line', x, y0 + 1, x, y0 + NOTE_AREA_H - 1, '#ffd54f', 1.2))
        prims.append(('text', x + 2, y0 + 8, bpm_text, '#ffd54f', 5.2,
                      'bold', 'normal', 'start'))

    # 音符 (多押合并为单个圆点, 全部统一为 tap 样式, 不区分类型, 不画横条)
    # 行末音符 (恰在行边界 beat 近似为 row_beats 整数倍) 在两处绘制:
    #   - 上一行行末 (beat_in_row = row_beats)
    #   - 当前行行首 (beat_in_row = 0)
    # 注意: beat 来自浮点累加, 需用容差判断边界, 不能用精确取模
    boundary_tol = 1e-3  # beat 单位容差 (小于 1/1000 拍)
    for event_index, ev in enumerate(events):
        b = ev.get('display_beat', ev['beat'])
        if b < 0:
            continue
        # 是否在行边界: b 近似为 row_beats 的整数倍 (容差内)
        k = round(b / row_beats)
        on_boundary = b > boundary_tol and abs(b - k * row_beats) < boundary_tol
        if on_boundary:
            # 画在上一行行末 (row = k-1, beat_in_row = row_beats)
            # 同时画在当前行行首 (row = k, beat_in_row = 0)
            rows_for_ev = []
            prev_row = k - 1
            if 0 <= prev_row < n_rows:
                rows_for_ev.append((prev_row, float(row_beats)))  # 行末
            if 0 <= k < n_rows and b < total_beats:
                rows_for_ev.append((k, 0.0))  # 行首
        else:
            if b >= total_beats:
                continue
            row = int(b // row_beats)
            rows_for_ev = [(row, b - row * row_beats)]
        for row, beat_in_row in rows_for_ev:
            y0 = row * ROW_H
            x = beat_to_x_in_row(beat_in_row)
            cy = y0 + NOTE_CY
            nv = ev['nv']
            col = nv_color(nv)
            is_dotted = ev.get('style_label', ev['nv_label']).endswith('.')
            tup = tuplet_judge(nv) if nv not in (None, '-') else 1
            event_notes = ev.get('notes') or []
            ring_color, ring_dash = _event_ring_style(event_notes)

            # 音符头: 统一圆点
            if is_dotted:
                prims.append(('diamond', x, cy, NOTE_R, col, 'none', 0))
                prims.append(('diamond_ring', x, cy, NOTE_RING_R, ring_color, NOTE_RING_W, ring_dash))
            else:
                prims.append(('circle', x, cy, NOTE_R, col, 'none', 0))
                prims.append(('ring', x, cy, NOTE_RING_R, ring_color, NOTE_RING_W, ring_dash))

            # 连音数 (写在圆点中心)
            if tup != 1 and nv not in (None, '-'):
                prims.append(('text', x, cy, str(tup), '#ffffff', 8.0, 'bold', 'italic', 'middle'))
            # 时值标注 (白底区) — 每个绘制位置都标
            lbl = ev['nv_label']
            lc, lw, ls = _label_style(lbl, nv)
            label_size = label_sizes[event_index]
            prims.append(('text', x, y0 + LABEL_CY, lbl, lc, label_size, lw, ls, 'middle'))

    return prims, n_rows


def _label_style(lbl, nv):
    if lbl.endswith('.'):
        return ('#dd1313', 'bold', 'normal')
    if '/' in lbl:
        return ('#7f7f7f', 'bold', 'normal')
    if lbl in ('-', ''):
        return ('#7f7f7f', 'bold', 'normal')
    t = tuplet_judge(nv)
    if t == 3:
        return ('#2020ff', 'bold', 'normal')
    if t == 5:
        return ('#8F2874', 'bold', 'normal')
    if t == 7:
        return ('#7C3ED3', 'bold', 'normal')
    if t == 9:
        return ('#396E72', 'bold', 'normal')
    if t != 1:
        return ('#7f7f7f', 'bold', 'normal')
    return ('#111111', 'bold', 'normal')


def _fit_label_font_sizes(events):
    """Fit adjacent label pairs while leaving common values at the larger size."""
    sizes = [13.2 for _ in events]
    for _ in range(3):
        for index in range(len(events) - 1):
            left_beat = events[index].get('display_beat', events[index]['beat'])
            right_beat = events[index + 1].get('display_beat', events[index + 1]['beat'])
            gap = (right_beat - left_beat) * PX_PER_BEAT
            if gap <= 0:
                continue
            left_length = max(1, len(str(events[index]['nv_label'])))
            right_length = max(1, len(str(events[index + 1]['nv_label'])))
            estimated_sum = 0.64 * (
                left_length * sizes[index] + right_length * sizes[index + 1]
            )
            allowed_sum = max(2.0, gap * 1.65)
            if estimated_sum > allowed_sum:
                factor = allowed_sum / estimated_sum
                sizes[index] *= factor
                sizes[index + 1] *= factor
    return [max(7.0, min(13.2, size)) for size in sizes]


# ============ SVG 渲染 (长条, 可滚动) ============

def render_strip_svg(events, total_beats, bpm, chart, out_path, title,
                     row_beats=None, compact=False, meter_map=None):
    if row_beats is None:
        row_beats = int(math.ceil(total_beats))  # 不折叠, 单条超长
    prims, n_rows = build_primitives(events, row_beats, total_beats, bpm, chart, meter_map)
    W = row_width_px(row_beats)
    if compact:
        # 紧凑模式: 音符区 + 标注区 (无标题/行间距), 用于网页滚动
        H = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}" font-family="{SVG_TEXT_FONT_STACK}">']
        parts.append(f'<rect width="100%" height="100%" fill="#0a0a14"/>')
        for p in prims:
            if p[0] in ('rect', 'line', 'dot', 'circle', 'diamond', 'diamond_ring', 'star', 'tri', 'ring', 'text'):
                parts.append(_prim_to_svg(p, 0))
        parts.append('</svg>')
    else:
        H = n_rows * ROW_H + 26
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}" font-family="{SVG_TEXT_FONT_STACK}">']
        parts.append(f'<rect width="100%" height="100%" fill="#06060c"/>')
        parts.append(f'<text x="{W/2}" y="16" fill="#aaa" font-size="13" '
                     f'text-anchor="middle">{html.escape(title)}</text>')
        for p in prims:
            parts.append(_prim_to_svg(p, 22))
        parts.append('</svg>')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))


def strip_segment_paths(base_path, total_beats, segment_beats=SEGMENT_BEATS):
    segment_count = max(1, int(math.ceil(total_beats / segment_beats)))
    return [
        f'{base_path}_seg_{index:03d}.svg'
        for index in range(segment_count)
    ]


def _move_legacy_output(legacy_path, new_path, force=False):
    legacy_path = Path(legacy_path)
    new_path = Path(new_path)
    if force or new_path.exists() or not legacy_path.exists():
        return False
    new_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.replace(new_path)
    return True


def migrate_legacy_visual_outputs(song_dir, difficulty_id, force=False):
    """Move old flat visualization outputs into outputs/<difficulty>/ categories."""
    _move_legacy_output(
        legacy_difficulty_path(song_dir, difficulty_id, '_rhythm.svg'),
        rhythm_svg_path(song_dir, difficulty_id),
        force,
    )
    _move_legacy_output(
        legacy_difficulty_path(song_dir, difficulty_id, '_strip.svg'),
        strip_svg_path(song_dir, difficulty_id),
        force,
    )
    _move_legacy_output(
        legacy_difficulty_path(song_dir, difficulty_id, '_rhythm.png'),
        rhythm_png_path(song_dir, difficulty_id),
        force,
    )

    legacy_prefix = f'{difficulty_file_stem(difficulty_id)}_strip_seg_'
    segment_dir = strip_segment_base_path(song_dir, difficulty_id).parent
    segment_dir.mkdir(parents=True, exist_ok=True)
    for path in Path(song_dir).glob(f'{legacy_prefix}*.svg'):
        suffix = path.name[len(legacy_prefix):]
        target = segment_dir / f'strip_seg_{suffix}'
        _move_legacy_output(path, target, force)


def render_strip_svg_segments(events, total_beats, bpm, chart, base_path,
                              segment_beats=SEGMENT_BEATS, meter_map=None):
    """Render compact long-strip SVG segments for frontend virtualization.

    The full compact strip SVG is still generated as a fallback/debug artifact.
    Each segment keeps global SVG coordinates via viewBox so the frontend can
    position it at its natural x offset without losing vector clarity.
    """
    row_beats = int(math.ceil(total_beats))
    prims, _ = build_primitives(events, row_beats, total_beats, bpm, chart, meter_map)
    full_width = row_width_px(row_beats)
    height = NOTE_AREA_H + LABEL_GAP + LABEL_AREA_H
    content_start = PAD_X
    segment_width = segment_beats * PX_PER_BEAT
    segment_count = max(1, int(math.ceil(total_beats / segment_beats)))

    directory = os.path.dirname(base_path) or '.'
    prefix = os.path.basename(base_path) + '_seg_'
    for name in os.listdir(directory):
        if name.startswith(prefix) and name.endswith('.svg'):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass

    for index in range(segment_count):
        if index == 0:
            x0 = 0
            x1 = min(full_width, content_start + segment_width)
        else:
            x0 = content_start + index * segment_width
            x1 = min(full_width, content_start + (index + 1) * segment_width)
        width = max(1, x1 - x0)
        out_path = f'{base_path}_seg_{index:03d}.svg'
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="{x0} 0 {width} {height}" font-family="{SVG_TEXT_FONT_STACK}">'
        ]
        parts.append(f'<rect x="{x0:.2f}" y="0" width="{width:.2f}" height="{height:.2f}" fill="#0a0a14"/>')
        for p in prims:
            bounds = _prim_x_bounds(p)
            if bounds is None or (bounds[1] >= x0 - 80 and bounds[0] <= x1 + 80):
                if p[0] in ('rect', 'line', 'dot', 'circle', 'diamond', 'diamond_ring', 'star', 'tri', 'ring', 'text'):
                    parts.append(_prim_to_svg(p, 0))
        parts.append('</svg>')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(parts))


def _prim_x_bounds(p):
    k = p[0]
    if k == 'rect':
        _, x, _y, w, _h, _fill, _alpha = p
        return (x, x + w)
    if k == 'line':
        _, x1, _y1, x2, _y2, _stroke, sw = p
        return (min(x1, x2) - sw, max(x1, x2) + sw)
    if k == 'dot':
        _, x, _y, r, _fill = p
        return (x - r, x + r)
    if k == 'circle':
        _, x, _y, r, _fill, _stroke, sw = p
        return (x - r - sw, x + r + sw)
    if k in ('ring', 'diamond_ring'):
        _, x, _y, r, _stroke, sw, _dash = p
        return (x - r - sw, x + r + sw)
    if k == 'diamond':
        _, x, _y, r, _fill, _stroke, sw = p
        return (x - r - sw, x + r + sw)
    if k == 'star':
        _, x, _y, r, _fill = p
        return (x - r, x + r)
    if k == 'tri':
        _, x, _y, s, _fill = p
        return (x - s, x + s * 1.3)
    if k == 'text':
        _, x, _y, txt, _fill, size, _weight, _style, _anchor = p
        half = max(8, len(str(txt)) * size * 0.42)
        return (x - half, x + half)
    return None


def _prim_to_svg(p, yoff):
    k = p[0]
    if k == 'rect':
        _, x, y, w, h, fill, alpha = p
        a = f' fill-opacity="{alpha}"' if alpha != 1.0 else ''
        return f'<rect x="{x:.2f}" y="{y+yoff:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{fill}"{a}/>'
    if k == 'line':
        _, x1, y1, x2, y2, stroke, sw = p
        return f'<line x1="{x1:.2f}" y1="{y1+yoff:.2f}" x2="{x2:.2f}" y2="{y2+yoff:.2f}" stroke="{stroke}" stroke-width="{sw}"/>'
    if k == 'dot':
        _, x, y, r, fill = p
        return f'<circle cx="{x:.2f}" cy="{y+yoff:.2f}" r="{r}" fill="{fill}"/>'
    if k == 'circle':
        _, x, y, r, fill, stroke, sw = p
        return f'<circle cx="{x:.2f}" cy="{y+yoff:.2f}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    if k == 'ring':
        _, x, y, r, stroke, sw, dash = p
        d = f' stroke-dasharray="{dash}"' if dash else ''
        return f'<circle cx="{x:.2f}" cy="{y+yoff:.2f}" r="{r}" fill="none" stroke="{stroke}" stroke-width="{sw}"{d}/>'
    if k == 'diamond_ring':
        _, x, y, r, stroke, sw, dash = p
        d = f' stroke-dasharray="{dash}"' if dash else ''
        pts = diamond_path(x, y + yoff, r)
        pl = ' '.join(f'{px:.2f},{py:.2f}' for px, py in pts)
        return f'<polygon points="{pl}" fill="none" stroke="{stroke}" stroke-width="{sw}"{d}/>'
    if k == 'diamond':
        _, x, y, r, fill, stroke, sw = p
        pts = diamond_path(x, y + yoff, r)
        pl = ' '.join(f'{px:.2f},{py:.2f}' for px, py in pts)
        return f'<polygon points="{pl}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    if k == 'star':
        _, x, y, r, fill = p
        pts = star_path(x, y + yoff, r)
        pl = ' '.join(f'{px:.2f},{py:.2f}' for px, py in pts)
        return f'<polygon points="{pl}" fill="{fill}"/>'
    if k == 'tri':
        _, x, y, s, fill = p
        pts = [(x, y - s + yoff), (x, y + s + yoff), (x + s * 1.3, y + yoff)]
        pl = ' '.join(f'{px:.2f},{py:.2f}' for px, py in pts)
        return f'<polygon points="{pl}" fill="{fill}"/>'
    if k == 'text':
        _, x, y, txt, fill, size, weight, style, anchor = p
        return (f'<text x="{x:.2f}" y="{y+yoff:.2f}" fill="{fill}" font-size="{size}" '
                f'font-family="{SVG_TEXT_FONT_STACK}" font-weight="{weight}" font-style="{style}" '
                f'text-anchor="{anchor}" dominant-baseline="middle">'
                f'{html.escape(str(txt))}</text>')
    return ''


# ============ matplotlib 折叠 PNG 渲染 ============

def render_strip_png(events, total_beats, bpm, chart, out_path, title,
                     row_beats=ROW_BEATS_DEFAULT, dpi=150, meter_map=None):
    prims, n_rows = build_primitives(events, row_beats, total_beats, bpm, chart, meter_map)
    W = row_width_px(row_beats)
    H = n_rows * ROW_H + 26
    fig_w = W / 100.0
    fig_h = H / 100.0
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis('off')
    fig.patch.set_facecolor('#06060c')

    ax.text(W / 2, 13, title, color='#aaa', fontsize=11, ha='center', va='center')
    for p in prims:
        _draw_prim_mpl(ax, p, 22)

    fig.savefig(out_path, dpi=dpi, facecolor=fig.get_facecolor(), bbox_inches=None)
    plt.close(fig)


def _draw_prim_mpl(ax, p, yoff):
    k = p[0]
    if k == 'rect':
        _, x, y, w, h, fill, alpha = p
        ax.add_patch(Rectangle((x, y + yoff), w, h, facecolor=fill,
                               edgecolor='none', alpha=alpha, linewidth=0))
    elif k == 'line':
        _, x1, y1, x2, y2, stroke, sw = p
        ax.plot([x1, x2], [y1 + yoff, y2 + yoff], color=stroke,
                linewidth=sw, solid_capstyle='butt')
    elif k == 'dot':
        _, x, y, r, fill = p
        ax.add_patch(Circle((x, y + yoff), r, facecolor=fill, edgecolor='none'))
    elif k == 'circle':
        _, x, y, r, fill, stroke, sw = p
        ax.add_patch(Circle((x, y + yoff), r, facecolor=fill,
                            edgecolor=stroke, linewidth=sw))
    elif k == 'ring':
        _, x, y, r, stroke, sw, dash = p
        ls = (0, (3, 2)) if dash else '-'
        ax.add_patch(Circle((x, y + yoff), r, facecolor='none',
                            edgecolor=stroke, linewidth=sw, linestyle=ls))
    elif k == 'diamond_ring':
        _, x, y, r, stroke, sw, dash = p
        pts = diamond_path(x, y + yoff, r)
        ls = (0, (3, 2)) if dash else '-'
        ax.add_patch(Polygon(pts, closed=True, facecolor='none',
                             edgecolor=stroke, linewidth=sw, linestyle=ls))
    elif k == 'diamond':
        _, x, y, r, fill, stroke, sw = p
        pts = diamond_path(x, y + yoff, r)
        ax.add_patch(Polygon(pts, closed=True, facecolor=fill,
                             edgecolor=stroke, linewidth=sw))
    elif k == 'star':
        _, x, y, r, fill = p
        pts = star_path(x, y + yoff, r)
        ax.add_patch(Polygon(pts, closed=True, facecolor=fill, edgecolor='none'))
    elif k == 'tri':
        _, x, y, s, fill = p
        pts = [(x, y - s + yoff), (x, y + s + yoff), (x + s * 1.3, y + yoff)]
        ax.add_patch(Polygon(pts, closed=True, facecolor=fill, edgecolor='none'))
    elif k == 'text':
        _, x, y, txt, fill, size, weight, style, anchor = p
        ha = {'middle': 'center', 'start': 'left', 'end': 'right'}.get(anchor, 'center')
        fw = 'bold' if weight == 'bold' else 'normal'
        fs = 'italic' if style == 'italic' else 'normal'
        ax.text(x, y + yoff, str(txt), color=fill, fontsize=size * 1.1,
                ha=ha, va='center', fontweight=fw, fontstyle=fs, fontfamily=MPL_TEXT_FONT_FAMILY)


# ============ 发现歌曲 & 处理 ============

def process_song(song_dir, song_id, force=False, difficulties=None):
    """
    处理单首歌曲: 读取 maidata.txt → 解析谱面 → 输出 SVG/PNG。
    输出四类文件:
      outputs/{难度}/rhythm/rhythm.svg — 完整节奏长条图 (含标题, 不折叠)
      outputs/{难度}/strip/strip.svg — 紧凑版长条图 (仅音符区, 用于网页嵌入)
      outputs/{难度}/strip/segments/strip_seg_*.svg — 分段 SVG (用于网页虚拟化滚动)
      outputs/{难度}/rhythm/rhythm.png — 折叠版 PNG (每32拍一行, 便于浏览)
    """
    mp = os.path.join(song_dir, 'maidata.txt')
    if not os.path.exists(mp):
        return {'song_id': song_id, 'error': 'no maidata.txt'}
    try:
        song = parse_maidata(mp)
    except Exception as e:
        return {'song_id': song_id, 'error': f'parse: {e}'}

    selected_difficulties = (sorted(song.charts) if difficulties is None
                             else [did for did in difficulties if did in song.charts])
    stats = {'song_id': song_id, 'title': song.title, 'artist': song.artist,
             'bpm': song.bpm, 'difficulties': {}, 'errors': []}
    for did in selected_difficulties:
        ch = song.charts[did]
        if not ch.notes:
            continue
        migrate_legacy_visual_outputs(song_dir, did, force=force)
        segment_base = strip_segment_base_path(song_dir, did)
        events = compute_rhythm_events(ch)
        # 先分析拍号，再让所有输出共用同一份小节时间轴。尾部长度也按末尾拍号计算。
        last_note_beat = time_to_beat(max(n.time_sec for n in ch.notes), ch.bpm_timeline)
        meter_map = analyze_chart_meter(
            song_dir, did, ch, last_note_beat, song.first_offset, force=False,
        )
        folded_total_beats = meter_map.add_measures(last_note_beat, 1)
        long_total_beats = meter_map.add_measures(
            folded_total_beats, LONG_IMAGE_EXTRA_MEASURES,
        )
        # BPM 摘要文本
        chart_bpms = sorted({value for _, value in ch.bpm_timeline})
        bpm_summary = (f'BPM {chart_bpms[0]:g}' if len(chart_bpms) == 1 else
                       f'BPM {chart_bpms[0]:g}-{chart_bpms[-1]:g} (VAR)')
        title = (f'{song.title} — {DIFFICULTY_NAMES.get(did, did)} (Lv.{ch.level})  '
                 f'{bpm_summary}')
        # 1) 完整长条 SVG (不折叠, 含标题+标注)
        rhythm_svg = rhythm_svg_path(song_dir, did)
        rhythm_svg.parent.mkdir(parents=True, exist_ok=True)
        if force or not os.path.exists(rhythm_svg):
            try:
                render_strip_svg(events, long_total_beats, song.bpm, ch,
                                 str(rhythm_svg), title, meter_map=meter_map)
            except Exception as e:
                print(f'    SVG warn: {e}')
                stats['errors'].append(f'{did} rhythm SVG: {e}')
        # 2) 紧凑 SVG (仅音符+标注区, 无标题/行间距, 用于 make_html.py 嵌入)
        strip_svg = strip_svg_path(song_dir, did)
        strip_svg.parent.mkdir(parents=True, exist_ok=True)
        if force or not os.path.exists(strip_svg):
            try:
                render_strip_svg(events, long_total_beats, song.bpm, ch,
                                 str(strip_svg), title, compact=True, meter_map=meter_map)
            except Exception as e:
                print(f'    compact SVG warn: {e}')
                stats['errors'].append(f'{did} strip SVG: {e}')
        # 2.5) 紧凑 SVG 分段 (前端优先虚拟化加载；完整 strip 作为 fallback)
        segment_base.parent.mkdir(parents=True, exist_ok=True)
        expected_segments = strip_segment_paths(str(segment_base), long_total_beats)
        if force or any(not os.path.exists(path) for path in expected_segments):
            try:
                render_strip_svg_segments(events, long_total_beats, song.bpm, ch,
                                          str(segment_base), meter_map=meter_map)
            except Exception as e:
                print(f'    compact SVG segment warn: {e}')
                stats['errors'].append(f'{did} strip SVG segments: {e}')
        # 3) 折叠 PNG (每32拍一行, 方便直接查看)
        rhythm_png = rhythm_png_path(song_dir, did)
        rhythm_png.parent.mkdir(parents=True, exist_ok=True)
        if force or not os.path.exists(rhythm_png):
            try:
                render_strip_png(events, folded_total_beats, song.bpm, ch,
                                 str(rhythm_png), title, meter_map=meter_map)
            except Exception as e:
                print(f'    PNG warn: {e}')
                stats['errors'].append(f'{did} rhythm PNG: {e}')
        stats['difficulties'][did] = {'level': ch.level, 'notes': len(ch.notes)}
    return stats


def print_summary(results):
    print(f"\n{'=' * 72}")
    print(f"{'ID':<10} {'Title':<30} {'BSC':<6} {'ADV':<6} {'EXP':<6} {'MST':<6}")
    print(f"{'=' * 72}")
    total = 0
    for s in results:
        if s.get('error'):
            print(f"{s['song_id']:<10} ERROR: {s['error']}")
            continue
        cnts = {d: s['difficulties'][d]['notes'] for d in s['difficulties']}
        total += sum(cnts.values())
        print(f"{s['song_id']:<10} {s['title'][:28]:<30} "
              f"{cnts.get(2, 0):<6} {cnts.get(3, 0):<6} {cnts.get(4, 0):<6} {cnts.get(5, 0):<6}")
    print(f"{'=' * 72}")
    print(f"Total: {len(results)} songs, {total} notes")


def main():
    """
    主入口: 自动发现歌曲 → 逐个处理 → 打印汇总。
    用法:
      python visualize.py               # 处理脚本所在目录所有歌曲
      python visualize.py -i ./songs    # 指定歌曲根目录
      python visualize.py -d 11391      # 只处理指定曲目
      python visualize.py -f            # 强制覆盖已有输出
    """
    ap = argparse.ArgumentParser(description='maimai 节奏可视化 (Arcaea 风格)')
    ap.add_argument('-i', '--input', default=None, help='歌曲根目录')
    ap.add_argument('-d', '--dir', default=None, help='只处理指定曲目名')
    ap.add_argument('-diff', '--difficulty', type=int, default=None,
                    help='难度 ID；不指定则默认只处理 MASTER/Re:MASTER')
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
    results = []
    failures = 0
    for sd, sid in songs:
        print(f'  [{sid}] 处理中...', end=' ')
        try:
            if args.difficulty is not None:
                target_difficulties = [args.difficulty]
            else:
                target_difficulties = default_target_difficulties(parse_maidata(os.path.join(sd, 'maidata.txt')).charts)
            r = process_song(sd, sid, args.force, target_difficulties)
            results.append(r)
            if r.get('error') or r.get('errors'):
                failures += 1
            dl = ', '.join(DIFFICULTY_NAMES.get(d, str(d)) for d in sorted(r['difficulties']))
            print(f'✓ {r["title"]} ({dl})')
        except Exception as e:
            print(f'✗ {e}'); results.append({'song_id': sid, 'error': str(e)})
            failures += 1
    if results:
        print_summary(results)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

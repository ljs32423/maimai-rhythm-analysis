#!/usr/bin/env python3
"""
Simai 谱面解析器
=================
将 maimai simai 格式的 maidata.txt 解析为结构化数据。

Simai 格式速览:
  头部: &title=..., &artist=..., &wholebpm=..., &first=..., &lv_N=..., &inote_N=...
  谱面体 (inote):
    (BPM)            — 设定当前 BPM
    {N}              — 设定每拍等分数 (4=16分, 8=32分...)
    {#sec}           — 直接指定每个逗号步进的秒数
    1-8              — Tap (按钮编号)
    1b, 1x, 1h, 1hx — Break / Ex / Hold / Ex-Hold
    1-4[分:拍]       — 直滑条 (从1滑到4)
    1<4, 1>4, 1p4, 1q4, 1z4, 1s4, 1v4, 1V4, 1w4 — 方向滑条
    1qq4, 1pp4       — 双字滑条 (大弧)
    Ch, Cf, B1-B8, A1-A8, D1-D8, E1-E8 — Touch (传感器区域)
    E                — 谱面结束
    ` (反引号)        — 伪 EACH, +0.001s 偏移
    $ / @             — 星星 / 无星标记
    ? / !             — 滑条星星变体

时长写法:
  [a:b]              — a分b拍 (例 [1:1]=1拍, [8:3]=3/8拍)
  [BPM#a:b]          — 指定BPM的a分b拍
  [#sec]             — 秒数 (例 [#0.5]=0.5秒)
  [wait##travel]     — 滑条蓄力+移动分离写法

关键修正 (v6):
  - comma 时值公式: 240/(BPM*division)  (原 (60/BPM)/division 差 4 倍)
  - [a:b] 时值公式: 240*b/(a*BPM)      (原 a/b*60/BPM 当 b!=1 时错误)
  - BPM 变化时间线 chart.bpm_timeline 用于精确拍位换算
  - TOUCH 传感器 A/B/C/D/E 全部识别
  - 连接滑条 (1-4q7-2[1:2]) 与同始点滑条 (1-4[4:3]*-6[8:5])
"""
import re
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional
from collections import Counter


class NoteType:
    """音符类型常量"""
    TAP = "tap"
    HOLD = "hold"
    BREAK = "break"          # 大触 (黄色)
    EX = "ex"                # 绝赞 (紫色)
    SLIDE = "slide"          # 星星/滑条
    TOUCH = "touch"          # 触摸 (屏幕传感器)
    TOUCH_HOLD = "touch_hold"  # 触摸长按
    FIREWORK = "firework"    # 触摸烟花 (仅作兼容, 实际用 extra['firework'])
    SWEEP_MARKER = "sweep_marker"  # maidata_sweep.txt 人工扫键头标记


# --- 滑条操作符集合 ---
# 单字: - 直滑, < 左弧, > 右弧, ^ V形上, p 顺时, q 逆时, z 折线,
#        s 星形, v 小V, V 大V(带通过点), w 扇形
SLIDE_OPS_SINGLE = frozenset('-<>^pqzsvVw')
# 双字: qq 大逆时弧, pp 大顺时弧
SLIDE_OPS_DOUBLE = frozenset(['qq', 'pp'])
# 无长度 HOLD 的默认等效分拍 (3simai 约定: [1280:1])
DEFAULT_HOLD_DIV = 1280


@dataclass
class Note:
    """单个音符"""
    note_type: str                               # 类型，见 NoteType
    button: int                                  # 按钮编号 1-8; TOUCH 用 0 (真实传感器存 extra['sensor'])
    time_sec: float                              # 绝对时间(秒)，以谱面开头为0
    duration_sec: float = 0.0                    # 持续时间(秒): HOLD=按住时长, SLIDE=wait+travel总长
    end_button: int = 0                          # 滑条终点按钮
    extra: dict = field(default_factory=dict)    # 附加属性: is_ex, is_break, star, firework, sensor, slide_op, path ...

    @property
    def is_ex(self) -> bool:
        """是否为 EX (绝赞) 音符"""
        return bool(self.extra.get('is_ex'))

    @property
    def is_break(self) -> bool:
        """是否为 Break (大触) 音符"""
        return self.note_type == NoteType.BREAK or bool(self.extra.get('is_break'))

    @property
    def is_firework(self) -> bool:
        """是否为烟花触摸"""
        return bool(self.extra.get('firework'))

    @property
    def is_star(self) -> bool:
        """是否带星星标记"""
        return bool(self.extra.get('star'))


@dataclass
class Chart:
    """单个难度的谱面"""
    level: str | float                            # 等级显示文本 (如 12.9 / 10+)
    designer: str                                 # 谱师
    notes: List[Note] = field(default_factory=list)
    bpm_timeline: List[Tuple[float, float]] = field(default_factory=list)  # BPM变化时间线: [(t_start_sec, bpm), ...]
    init_division: int = 4                        # 初始等分数 (来自第一个 {N})


@dataclass
class SongData:
    """完整的歌曲数据"""
    title: str                                    # 曲名
    artist: str                                   # 艺术家
    bpm: float                                    # 默认 BPM (来自 &wholebpm)
    first_offset: float                           # 谱面延迟 (来自 &first)
    genre: str                                    # 分类
    version: str                                  # 版本
    charts: dict = field(default_factory=dict)    # {difficulty_id: Chart}


# ============ 时长解析 ============
# 以下函数将 simai 括号内的时长规格转换为秒。
# 支持格式: [a:b], [BPM#a:b], [#sec], [wait##travel]
#
# 核心公式:
#   [a:b]    = 240 * b / (a * BPM)  秒
#   推导: 1拍 = 60/BPM 秒 = 4个四分音符
#         240/BPM = 4拍时长(秒)
#         a分b = b/a 拍 → 240*b/(a*BPM) 秒
#   {N} comma = 240/(BPM * N) 秒


def _part_bpm(part: str, cur_bpm: float) -> float:
    """
    提取 `BPM#...` 前缀的 BPM 值。
    若没有 # 则返回当前 BPM (cur_bpm)。
    例: "150#1:1" → 150.0
    """
    part = part.strip()
    if '#' in part:
        left = part.split('#', 1)[0].strip()
        if left:
            try: return float(left)
            except: return cur_bpm
    return cur_bpm


def _dur_of(part: str, cur_bpm: float) -> float:
    """
    解析单段时长 → 秒。
    支持: `a:b` (分拍), `BPM#a:b`, `sec` (秒数), `BPM#sec`, `#sec`
    """
    part = part.strip()
    if not part:
        return 0.0
    bpm = cur_bpm
    rest = part
    if '#' in part:
        left, right = part.split('#', 1)
        if left.strip():
            try: bpm = float(left.strip())
            except: pass
        rest = right.strip()
    if ':' in rest:
        # a分b拍 → 秒: 240 * b / (a * BPM)
        a, b = rest.split(':', 1)
        try:
            a_f, b_f = float(a), float(b)
            if a_f <= 0 or b_f <= 0: return 0.0
            return 240.0 * b_f / (a_f * max(bpm, 1e-6))
        except: return 0.0
    else:
        try: return float(rest)
        except: return 0.0


def parse_hold_duration(spec: str, cur_bpm: float) -> float:
    """
    解析 HOLD 时长 → 秒。
    HOLD 无蓄力(wait)概念，直接返回单段时长。
    """
    return _dur_of(spec, cur_bpm)


def parse_slide_duration(spec: str, cur_bpm: float) -> Tuple[float, float, float]:
    """
    解析 SLIDE 时长 → (总时长, 蓄力时间, 移动时间) 三元素。
    形式:
      [travel]           — 仅移动时间，蓄力默认 1 拍
      [wait##travel]     — 蓄力+移动分离写法
    travel/wait 支持: a:b / BPM#a:b / sec / BPM#sec
    """
    spec = spec.strip()
    if '##' in spec:
        w, t = spec.split('##', 1)
        wait = _dur_of(w, cur_bpm)
        travel = _dur_of(t, cur_bpm)
        return (wait + travel, wait, travel)
    travel = _dur_of(spec, cur_bpm)
    tbpm = _part_bpm(spec, cur_bpm)
    wait = 60.0 / max(tbpm, 1e-6)  # 默认蓄力 = 1 拍
    return (wait + travel, wait, travel)


def parse_time_spec(spec: str) -> float:
    """
    [已弃用] 兼容旧接口: a:b → 分数。
    新代码应使用 _dur_of()。
    """
    parts = spec.split(':')
    if len(parts) == 2:
        try: return float(parts[0]) / float(parts[1])
        except: pass
    return 0


# ============ 拍位换算 ============

def time_to_beat(t: float, timeline: List[Tuple[float, float]]) -> float:
    """
    秒 → 拍数 (考虑 BPM 变化)。
    遍历 BPM 时间线，累加每段的拍数。
    例: 谱面开始 t=0 BPM=210, t=5.0 BPM=105
        time_to_beat(7.0, ...) = 5.0*210/60 + 2.0*105/60 拍
    """
    if not timeline:
        return 0.0
    beat = 0.0
    for i, (t0, b) in enumerate(timeline):
        t1 = timeline[i + 1][0] if i + 1 < len(timeline) else float('inf')
        if t <= t0:
            break
        seg_end = min(t, t1)
        beat += (seg_end - t0) * b / 60.0  # 该段内的拍数
        if t <= t1:
            break
    return beat


# ============ maidata 字段提取 ============

def extract_field(content: str, name: str) -> str:
    """
    提取 &name= 到下一个 & 或文件末尾的多行值。
    用于 inote 字段 (跨多行)。
    """
    m = re.search(rf'&{name}=(.+?)(?:\n&|$)', content, re.DOTALL)
    return m.group(1).strip() if m else ''


def extract_line(content: str, name: str) -> str:
    """
    提取 &name= 所在行的值 (单行)。
    用于 title/artist/bpm/lv_N/des_N 等字段。
    """
    m = re.search(rf'&{name}=([^\n]+)', content)
    return m.group(1).strip() if m else ''


def parse_level_text(level_text: str) -> str:
    """
    解析 maidata 的等级字段为显示文本。
    兼容 10+ / 13+ / 14.5 这类写法，保留原始可读形式。
    """
    text = (level_text or '').strip()
    return text or '0'


def parse_maidata(filepath: str) -> SongData:
    """
    解析 maidata.txt 文件 → SongData。
    读取头部字段 → 按难度解析 inote → 组装完成。
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    song = SongData(
        title=extract_line(content, 'title'),
        artist=extract_line(content, 'artist'),
        bpm=float(extract_line(content, 'wholebpm') or 120),
        first_offset=float(extract_line(content, 'first') or 0),
        genre=extract_line(content, 'genre'),
        version=extract_line(content, 'version'),
    )
    # 支持 7 个难度 (1=EASY, 2-5=常规, 6=Re:MASTER, 7=UTOPIA)
    for did in (1, 2, 3, 4, 5, 6, 7):
        lv_s = extract_line(content, f'lv_{did}')
        des_s = extract_line(content, f'des_{did}')
        inote_s = extract_field(content, f'inote_{did}')
        if not inote_s:
            continue
        chart = Chart(level=parse_level_text(lv_s), designer=des_s or '')
        chart.notes, chart.bpm_timeline, chart.init_division = parse_inote(inote_s, song.bpm)
        song.charts[did] = chart
    return song


# ============ inote 主解析 ============

def parse_inote(text: str, bpm: float) -> Tuple[List[Note], List[Tuple[float, float]], int]:
    """
    解析谱面体 (inote) → (音符列表, BPM时间线, 初始等分数)。

    主循环逐个字符推进:
      (BPM)  — 更新 BPM 并记录时间线
      {N}    — 更新等分数
      {#sec} — 设定逗号步进秒数
      ,      — 时间推进
      E      — 谱面结束 (但 E1-E8 等是 TOUCH 传感器)
      /      — 同时押分隔符 (由 parse_group 处理)
      数字等 — 调用 parse_group 解析一组音符
    """
    text = text.replace('\n', '').replace('\r', '').strip()
    if not text:
        return ([], [(0.0, bpm)], 4)

    notes: List[Note] = []
    timeline: List[Tuple[float, float]] = [(0.0, bpm)]
    division = 4               # 当前等分数 (默认 {4}=16分)
    init_division = 4          # 记录初始等分数
    comma_override: Optional[float] = None  # {#sec} 指定的逗号秒数
    cur = 0.0                  # 当前时间 (秒)
    i = 0                      # 字符位置
    ended = False

    def cur_comma_sec() -> float:
        """计算一个逗号的时间步长"""
        if comma_override is not None:
            return comma_override
        return 240.0 / (max(bpm, 1e-6) * max(division, 1))

    while i < len(text):
        ch = text[i]
        # 跳过空白
        if ch in ' \t':
            i += 1
            continue
        # --- BPM 变化: (BPM) ---
        if ch == '(':
            end = text.find(')', i)
            if end == -1: break
            try:
                new_bpm = float(text[i + 1:end])
                # 记录 BPM 时间线 (去重: 同一时刻同一 BPM 不重复添加)
                if not timeline or timeline[-1][0] != cur or timeline[-1][1] != new_bpm:
                    if timeline and timeline[-1][0] == cur:
                        timeline[-1] = (cur, new_bpm)
                    else:
                        timeline.append((cur, new_bpm))
                bpm = new_bpm
            except: pass
            i = end + 1
            continue
        # --- 拍子分割: {N} 或 {#sec} ---
        if ch == '{':
            end = text.find('}', i)
            if end == -1: break
            inner = text[i + 1:end].strip()
            if inner.startswith('#'):
                # {#0.5} → 每个逗号 = 0.5 秒
                try: comma_override = float(inner[1:])
                except: pass
            else:
                # {8} → division = 8 (32分音符)
                try:
                    division = int(inner)
                    if init_division == 4 and division != 4:
                        init_division = division
                    comma_override = None
                except: pass
            i = end + 1
            continue
        # --- 逗号: 时间推进一步 ---
        if ch == ',':
            cur += cur_comma_sec()
            i += 1
            continue
        # --- E: 结束标记 或 E 传感器 ---
        if ch == 'E':
            # E 后跟数字/字母 h/f → TOUCH 传感器 E; 否则谱面结束
            nxt = text[i + 1] if i + 1 < len(text) else ''
            if nxt.isdigit() or nxt in ('h', 'f'):
                end, ns = parse_one(text, i, cur, bpm)
                if end > i:
                    notes.extend(ns); i = end
                    continue
            ended = True
            break
        # --- 斜杠: 已由 parse_group 处理，此处跳过 ---
        if ch == '/':
            i += 1
            continue
        # --- 尝试解析一组音符 (可能含 / 同时押) ---
        end, new_notes = parse_group(text, i, cur, bpm)
        if end > i:
            notes.extend(new_notes)
            i = end
        else:
            i += 1

    # 按时间和按钮排序
    notes.sort(key=lambda n: (n.time_sec, n.button))
    return (notes, timeline, init_division)


def parse_group(text: str, pos: int, cur: float, bpm: float) -> Tuple[int, List[Note]]:
    """
    解析一个逗号步进内的音符组 (可能含 `/` 分隔的同时押)。
    `/` 分隔的音符共享同一个逗号时间，`\`` 反引号微调 0.001s 偏移 (伪 EACH)。
    """
    notes: List[Note] = []
    i = pos
    base_cur = cur
    micro = 0.0  # ` 伪 EACH 累计偏移 (每个 ` 加 0.001s)
    while i < len(text):
        while i < len(text) and text[i] in ' \t': i += 1
        if i >= len(text):
            break
        ch = text[i]
        if ch in (',', 'E', '(', '{', '}'):
            break
        if ch == '/':
            i += 1
            micro = 0.0  # 新音符重置偏移
            continue
        if ch == '`':
            micro += 0.001  # 每个反引号 +0.001s
            i += 1
            continue
        end, ns = parse_one(text, i, base_cur + micro, bpm)
        if end > i:
            notes.extend(ns)
            i = end
        else:
            i += 1
            break
    return (i, notes)


def parse_one(text: str, pos: int, cur: float, bpm: float) -> Tuple[int, List[Note]]:
    """
    解析单个音符 token (无 / 分隔)。
    根据首字符分派:
      S          → 人工扫键头标记
      C/B/A/D/E → Touch 触摸传感器
      1-8       → 按钮音符 (Tap/Hold/Break/Ex/Slide)
    """
    if pos >= len(text):
        return (pos, [])

    # maidata_sweep.txt 扩展标记。作为 `/S` 放在一个音符组内，
    # 只携带时间点，不参与普通谱面音符统计或绘制。
    if text[pos] == 'S':
        following = text[pos + 1] if pos + 1 < len(text) else ''
        if not following or following in ',/ \t':
            return (pos + 1, [Note(NoteType.SWEEP_MARKER, 0, cur)])

    # TOUCH 传感器: C / Cf / Ch / Chf / Bn / An / Dn / En (+ 可选 f/h)
    # E 特殊: E 后必须是数字或 h/f 才是 TOUCH，否则作为谱面结束
    if text[pos] in 'CBAD E'.replace(' ', ''):
        letter = text[pos]
        if letter == 'E':
            nxt = text[pos + 1] if pos + 1 < len(text) else ''
            if not (nxt.isdigit() or nxt in ('h', 'f')):
                return (pos, [])  # 不是 touch, 交给上层当结束
        return parse_touch(text, pos, cur, bpm)

    # 按钮音符: 数字 1-8 开头
    if text[pos].isdigit():
        return parse_digit(text, pos, cur, bpm)

    return (pos, [])


def parse_touch(text: str, pos: int, cur: float, bpm: float) -> Tuple[int, List[Note]]:
    """
    解析 TOUCH 触摸传感器音符。
    格式: C / Ch / Chf / Cf / B1-B8 / A1-A8 / D1-D8 / E1-E8 ...
    传感器区域: C(中心), A(左上), B(左下), D(右上), E(右下)
    后缀: f=烟花, h=长按, hf=烟花长按
    """
    letter = text[pos]          # 传感器字母 C/B/A/D/E
    i = pos + 1
    sensor_num = 0
    if letter == 'C':
        # 中心区有些谱会写作 C1。编号只是语法占位，不应残留给按钮解析。
        if i < len(text) and text[i].isdigit():
            i += 1
    else:
        # C 以外的传感器跟 1 位数字
        if i < len(text) and text[i].isdigit():
            sensor_num = int(text[i])
            i += 1
    sensor = f"{letter}{sensor_num}" if letter != 'C' else 'C'

    firework = False
    is_hold = False
    dur = 0.0
    # 读取后缀: f (烟花), h (长按, 可选后跟 x/f/[时长])
    while i < len(text):
        c = text[i]
        if c == 'f':
            firework = True; i += 1; continue
        if c == 'h':
            is_hold = True; i += 1
            if i < len(text) and text[i] == 'x':
                i += 1
            if i < len(text) and text[i] == 'f':
                firework = True; i += 1
            if i < len(text) and text[i] == '[':
                e = text.find(']', i)
                if e != -1:
                    dur = parse_hold_duration(text[i + 1:e], bpm)
                    i = e + 1
            continue
        break
    # 无时长 HOLD 默认 = 1/1280 拍
    if is_hold and dur == 0.0:
        dur = 240.0 * 1 / (DEFAULT_HOLD_DIV * max(bpm, 1e-6))

    extra = {'sensor': sensor, 'firework': firework}
    nt = NoteType.TOUCH_HOLD if is_hold else NoteType.TOUCH
    return (i, [Note(nt, 0, cur, dur, 0, extra)])


def parse_digit(text: str, pos: int, cur: float, bpm: float) -> Tuple[int, List[Note]]:
    """
    解析数字音符 (1-8) + 修饰 + 可选滑条后缀。

    修饰符号 (可按任意顺序叠加):
      b     — Break (大触, 黄色)
      x     — Ex (绝赞, 紫色)
      h     — Hold (长按), 后可选 x/b/[$]
      $     — 星星标记
      $$    — 旋转星
      @     — 无星标记

    滑条后缀 (可跟在任何修饰之后):
      -<>^pqzsvVw — 单字滑条操作符
      qq/pp       — 双字滑条操作符
      加 [分:拍]   — 时长
      加 *        — 同始点多分支滑条
    """
    # 读取按钮编号
    i = pos
    while i < len(text) and text[i].isdigit():
        i += 1
    btn = int(text[pos:i])
    if btn < 1 or btn > 8:
        return (i, [])

    ntype = NoteType.TAP
    is_ex = False
    is_break = False
    had_hold = False
    hold_dur = 0.0
    is_star = False
    no_star = False

    # 读取修饰符号 (b/x/h/$/@, h 后可跟 x/b/[时长])
    while i < len(text):
        c = text[i]
        if c == 'b' and ntype == NoteType.TAP:
            is_break = True; ntype = NoteType.BREAK; i += 1; continue
        if c == 'x' and not had_hold:
            is_ex = True; i += 1; continue
        if c == 'h':
            had_hold = True; ntype = NoteType.HOLD; i += 1
            if i < len(text) and text[i] == 'x':
                is_ex = True; i += 1
            if i < len(text) and text[i] == 'b':
                is_break = True; ntype = NoteType.BREAK; i += 1
            if i < len(text) and text[i] == '[':
                e = text.find(']', i)
                if e != -1:
                    hold_dur = parse_hold_duration(text[i + 1:e], bpm)
                    i = e + 1
            continue
        if c == 'x' and had_hold:
            is_ex = True; i += 1; continue
        if c == '$':
            is_star = True; i += 1
            if i < len(text) and text[i] == '$':
                is_star = True; i += 1  # $$ 旋转星
            continue
        if c == '@':
            no_star = True; i += 1; continue
        break

    # 无时长 HOLD → 默认 1/1280 拍
    if had_hold and hold_dur == 0.0:
        hold_dur = 240.0 * 1 / (DEFAULT_HOLD_DIV * max(bpm, 1e-6))

    # 尝试解析滑条后缀
    slide_end, slide_notes = try_slide(text, i, btn, cur, bpm)
    if slide_end > i:
        base_extra = {'is_ex': is_ex, 'star': is_star or (len(slide_notes) > 0 and not no_star),
                      'no_star': no_star}
        base_notes: List[Note] = []
        if had_hold:
            base_notes.append(Note(NoteType.HOLD, btn, cur, hold_dur, 0,
                                   {**base_extra, 'is_break': is_break}))
        else:
            base_notes.append(Note(ntype, btn, cur, 0, 0, base_extra))
        return (slide_end, base_notes + slide_notes)

    # 老式 V 速度修饰 (非 slide 情况)
    if i < len(text) and text[i] == 'V':
        i += 1
        while i < len(text) and text[i].isdigit(): i += 1
        if i < len(text) and text[i] == '[':
            e = text.find(']', i)
            if e != -1: i = e + 1

    extra = {'is_ex': is_ex, 'star': is_star, 'no_star': no_star,
             'is_break': is_break}
    if had_hold:
        return (i, [Note(NoteType.HOLD, btn, cur, hold_dur, 0, extra)])
    return (i, [Note(ntype, btn, cur, 0, 0, extra)])


def try_slide(text: str, pos: int, from_btn: int, cur: float, bpm: float) -> Tuple[int, List[Note]]:
    """
    尝试解析滑条后缀。如果当前位置不是滑条操作符，返回 (pos, [])。

    支持:
      单字操作符: -<>^pqzsvVw
      双字操作符: qq, pp
      ? 或 ! 前缀: 滑条星星变体 (在操作符前)
    V 特殊处理: V 后必须是多位数字才是 slide，单数字 1-8 是 velocity 标记
    """
    if pos >= len(text):
        return (pos, [])
    # 滑条星星变体 ? ! (在操作符之前)
    star_variant = ''
    while pos < len(text) and text[pos] in '?!':
        star_variant = text[pos]
        pos += 1
    if pos >= len(text):
        return (pos, [])
    # 双字操作符
    if text[pos:pos + 2] in SLIDE_OPS_DOUBLE:
        return slide_body(text, pos + 2, from_btn, cur, bpm, text[pos:pos + 2], star_variant)
    ch = text[pos]
    if ch in SLIDE_OPS_SINGLE:
        # V 特殊：后跟多位数字 → slide（通过点）；后跟单位数字 → velocity 标记
        if ch == 'V':
            if pos + 1 < len(text) and text[pos + 1].isdigit():
                if pos + 2 >= len(text) or not text[pos + 2].isdigit():
                    return (pos, [])
        return slide_body(text, pos + 1, from_btn, cur, bpm, ch, star_variant)
    return (pos, [])


def slide_body(text: str, pos: int, from_btn: int, cur: float, bpm: float,
               first_op: str, star_variant: str) -> Tuple[int, List[Note]]:
    """
    滑条主体解析。

    两种模式:
      连接滑条:  1-4q7-2[1:2]    — 操作符链 (多个操作符连写)
      同始点滑条: 1-4[4:3]*-6[8:5] — 用 * 连接多个从同一按钮出发的滑条

    每条滑条: op [通过点] 目标按钮 [时长]
      例: q7-2[1:2] → q 到 7, - 到 2, 时长 1:2
          V83-6[1:2] → V 形经过 8, 再到 3, - 到 6
    """
    i = pos

    def parse_one_seg(start: int, op: str) -> Tuple[int, Optional[dict]]:
        """解析一段滑条: [通过点] 目标按钮 [时长]"""
        j = start
        pass_btn = 0
        if op == 'V' and j < len(text) and text[j].isdigit():
            pass_btn = int(text[j]); j += 1
        ns = j
        while j < len(text) and text[j].isdigit():
            j += 1
        if j == ns:
            return (start, None)
        to_btn = int(text[ns:j])
        dur_total = wait = travel = 0.0
        if j < len(text) and text[j] == '[':
            e = text.find(']', j)
            if e != -1:
                dur_total, wait, travel = parse_slide_duration(text[j + 1:e], bpm)
                j = e + 1
        return (j, {'op': op, 'to': to_btn, 'pass': pass_btn,
                    'dur': dur_total, 'wait': wait, 'travel': travel})

    def next_op_is(j: int) -> Optional[str]:
        """检查当前位置是否为下一个操作符"""
        if j + 1 < len(text) and text[j:j + 2] in SLIDE_OPS_DOUBLE:
            return text[j:j + 2]
        if j < len(text) and text[j] in SLIDE_OPS_SINGLE:
            return text[j]
        return None

    # --- 第一条滑条 (含可能的连接 op 链) ---
    segs = []
    op = first_op
    while True:
        i, seg = parse_one_seg(i, op)
        if seg is None:
            break
        segs.append(seg)
        nop = next_op_is(i)
        if nop is None or nop == 'V':  # V 作为连接操作符慎用，保守停止
            break
        if nop == 'V':
            break
        op = nop
        i += len(op)
    if not segs:
        return (pos, [])

    durs = [s['dur'] for s in segs if s['dur'] > 0]
    total = sum(durs) if durs else 0.0
    path = [{'op': s['op'], 'to': s['to'], 'pass': s['pass']} for s in segs]
    notes = [Note(NoteType.SLIDE, from_btn, cur, total, segs[-1]['to'],
                  {'slide_op': segs[0]['op'], 'star_variant': star_variant,
                   'path': path, 'connected': len(segs) > 1})]

    # --- 同始点滑条: * op to [dur] ... ---
    # 从同一个起始按钮发射多条不同方向的滑条
    while i < len(text) and text[i] == '*':
        i += 1
        nop = next_op_is(i)
        if nop is None:
            break
        op = nop
        i += len(op)
        i, seg = parse_one_seg(i, op)
        if seg is None:
            break
        notes.append(Note(NoteType.SLIDE, from_btn, cur, seg['dur'], seg['to'],
                          {'slide_op': op, 'star_variant': star_variant,
                           'pass': seg['pass']}))
    return (i, notes)


# ============ CLI ============
# 直接运行本脚本可快速查看解析结果

if __name__ == '__main__':
    import sys
    path = (sys.argv[1] if len(sys.argv) > 1 else
            r'C:\Code\maimai-rhythm-analysis\songs\WiPE OUT MEMORIES\maidata.txt')
    song = parse_maidata(path)
    print(f"{song.title} | {song.artist} | BPM {song.bpm}")
    dnames = {1: "Easy", 2: "Basic", 3: "Advanced", 4: "Expert", 5: "Master", 6: "Re:Master", 7: "Utopia"}
    for did in sorted(song.charts):
        c = song.charts[did]
        if not c.notes: continue
        mt = max(n.time_sec for n in c.notes)
        print(f"\n[{dnames.get(did, did)}] Lv.{c.level}  谱师: {c.designer or '未知'}")
        print(f"  音符数: {len(c.notes)}   末尾: {mt:.2f}s ({mt / 60:.2f}min)")
        print(f"  BPM 时间线: {c.bpm_timeline[:5]}{' ...' if len(c.bpm_timeline) > 5 else ''}")
        print(f"  初始 division: {c.init_division}")
        # 音符类型分布统计
        types = Counter(n.note_type for n in c.notes)
        print(f"  分布: {', '.join(f'{k}:{v}' for k, v in types.most_common())}")
        # Slide 示例 (前5条)
        slides = [n for n in c.notes if n.note_type == NoteType.SLIDE]
        if slides:
            print(f"  Slide 示例 (前5):")
            for s in slides[:5]:
                op = s.extra.get('slide_op', '?')
                print(f"    {s.button} --{op}-> {s.end_button}  @{s.time_sec:.2f}s  "
                      f"dur={s.duration_sec:.2f}s")

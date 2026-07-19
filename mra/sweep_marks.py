"""人工扫键头标记文件的初始化、校验与事件应用。"""
from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .difficulty import sweep_maidata_path
from .simai_parser import NoteType, parse_inote, parse_maidata


EVENT_MATCH_TOLERANCE_SEC = 0.005 + 1e-9
_INOTE_FIELD_RE = re.compile(
    r'(^&inote_([1-7])=)(.*?)(?=^&|\Z)',
    re.MULTILINE | re.DOTALL,
)
_VALID_MARKER_RE = re.compile(r'/S(?=[,/\s]|$)')
_MARKER_LIKE_RE = re.compile(r'(?<![A-Za-z0-9])S[^,\s/]*')


@dataclass
class SweepApplyResult:
    path: Path
    created: bool = False
    stale: bool = False
    warnings: list[str] = field(default_factory=list)


def _append_marker(group: str) -> str:
    """在一个逗号音符组末尾插入 /S，同时保留原换行和空白。"""
    trailing_start = len(group.rstrip())
    core = group[:trailing_start]
    trailing = group[trailing_start:]
    separator = '' if core.endswith('/') else '/'
    return f'{core}{separator}S{trailing}'


def _annotate_inote(inote: str, bpm: float,
                    sweep_times: Iterable[float]) -> str:
    """把机器识别到的时间映射回原始逗号组，并加入 /S。"""
    targets = [float(value) for value in sweep_times]
    if not targets:
        return inote

    groups = inote.split(',')
    # 给每个逗号组临时加标记，再交给正式解析器计算其准确时间。这样 BPM、
    # {N}、{#sec} 与反引号等规则只保留一套实现。
    probe = ','.join(_append_marker(group) for group in groups)
    notes, _, _ = parse_inote(probe, bpm)
    group_markers = [
        note for note in notes if note.note_type == NoteType.SWEEP_MARKER
    ]

    marked_groups: set[int] = set()
    for target in targets:
        if not group_markers:
            break
        index = min(
            range(len(group_markers)),
            key=lambda pos: abs(group_markers[pos].time_sec - target),
        )
        if abs(group_markers[index].time_sec - target) <= EVENT_MATCH_TOLERANCE_SEC:
            # 结束标记 E 会让解析器停止，因此有效 marker 与前面的 group 顺序一一对应。
            marked_groups.add(index)

    return ','.join(
        _append_marker(group) if index in marked_groups else group
        for index, group in enumerate(groups)
    )


def _seed_sweep_markers(content: str, bpm: float,
                        sweep_times_by_difficulty: Mapping[int, Iterable[float]]) -> str:
    def annotate_field(match: re.Match) -> str:
        difficulty = int(match.group(2))
        return match.group(1) + _annotate_inote(
            match.group(3), bpm, sweep_times_by_difficulty.get(difficulty, ()),
        )

    return _INOTE_FIELD_RE.sub(annotate_field, content)


def ensure_sweep_maidata(
    song_dir: str | Path,
    sweep_times_by_difficulty: Mapping[int, Iterable[float]] | None = None,
) -> tuple[Path, bool]:
    """缺失时复制源谱并写入机器识别的 /S；已有人工文件永不覆盖。"""
    song_root = Path(song_dir)
    source = song_root / 'maidata.txt'
    output = sweep_maidata_path(song_root)
    if output.is_file():
        return output, False
    if not source.is_file():
        raise FileNotFoundError(f'Missing maidata.txt: {song_root}')

    if sweep_times_by_difficulty is None:
        shutil.copyfile(source, output)
    else:
        source_content = source.read_bytes().decode('utf-8')
        bpm = parse_maidata(str(source)).bpm
        seeded = _seed_sweep_markers(
            source_content, bpm, sweep_times_by_difficulty,
        )
        # 直接写字节，避免改变源文件原有的 LF/CRLF 与 UTF-8 BOM。
        output.write_bytes(seeded.encode('utf-8'))
    return output, True


def strip_sweep_markers(content: str) -> str:
    """移除 inote 字段里的 /S，供人工谱与源谱结构比较。"""
    def clean_field(match: re.Match) -> str:
        return match.group(1) + _VALID_MARKER_RE.sub('', match.group(3))

    return _INOTE_FIELD_RE.sub(clean_field, content)


def _malformed_marker_warnings(content: str) -> list[str]:
    warnings: list[str] = []
    for field_match in _INOTE_FIELD_RE.finditer(content):
        difficulty = field_match.group(2)
        for marker_match in _MARKER_LIKE_RE.finditer(field_match.group(3)):
            token = marker_match.group(0)
            if token != 'S':
                warnings.append(
                    f'难度 {difficulty} 存在无效扫键标记 {token!r}；请使用 /S',
                )
    return warnings


def _load_markers(path: Path, difficulty: int) -> tuple[list[float], list[str]]:
    content = path.read_text(encoding='utf-8')
    warnings = _malformed_marker_warnings(content)
    try:
        song = parse_maidata(str(path))
    except Exception as exc:
        return [], warnings + [f'无法解析 {path.name}: {exc}']
    chart = song.charts.get(difficulty)
    if chart is None:
        return [], warnings + [f'{path.name} 中没有难度 {difficulty}']
    markers = [
        float(note.time_sec)
        for note in chart.notes
        if note.note_type == NoteType.SWEEP_MARKER
    ]
    return markers, warnings


def _nearest_event_index(events: list[dict], time_sec: float) -> int | None:
    if not events:
        return None
    index = min(range(len(events)), key=lambda pos: abs(float(events[pos]['time']) - time_sec))
    if abs(float(events[index]['time']) - time_sec) <= EVENT_MATCH_TOLERANCE_SEC:
        return index
    return None


def apply_sweep_maidata(events: list[dict], song_dir: str | Path,
                         difficulty: int) -> SweepApplyResult:
    """完全以 maidata_sweep.txt 中是否存在 /S 决定扫键头标记。"""
    song_root = Path(song_dir)
    machine_times = [
        float(event['time']) for event in events if event.get('is_sweep_start')
    ]
    path, created = ensure_sweep_maidata(
        song_root, {difficulty: machine_times},
    )
    result = SweepApplyResult(path=path, created=created)

    # 人工文件是唯一真值；机器结果只用于文件首次创建时的初始填充。
    for event in events:
        event['is_sweep_start'] = False

    source_content = (song_root / 'maidata.txt').read_text(encoding='utf-8')
    marker_content = path.read_text(encoding='utf-8')
    result.stale = strip_sweep_markers(marker_content) != source_content
    if result.stale:
        result.warnings.append(
            f'{path.name} 除 /S 外已与 maidata.txt 不一致；保留人工文件并按现有拍位应用',
        )

    markers, parse_warnings = _load_markers(path, difficulty)
    result.warnings.extend(parse_warnings)
    matched_indexes: list[int] = []
    for marker_time in markers:
        event_index = _nearest_event_index(events, marker_time)
        if event_index is None:
            result.warnings.append(
                f'难度 {difficulty} 的 /S（{marker_time:.6f}s）没有对应音符事件，已忽略',
            )
            continue
        matched_indexes.append(event_index)

    for event_index, count in sorted(Counter(matched_indexes).items()):
        if count > 1:
            result.warnings.append(
                f'难度 {difficulty} 的 {events[event_index]["time"]:.6f}s 存在重复 /S；按一个标记处理',
            )
        events[event_index]['is_sweep_start'] = True
    return result

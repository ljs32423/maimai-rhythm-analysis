"""人工扫键头标记文件的初始化、校验与事件应用。"""
from __future__ import annotations

import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .difficulty import legacy_sweep_maidata_path, sweep_maidata_path
from .meter import MeterMap, MeterMeasure
from .simai_parser import (Note, NoteType, parse_inote, parse_maidata,
                           parse_maidata_content, time_to_beat)


EVENT_MATCH_TOLERANCE_SEC = 0.005 + 1e-9
_INOTE_FIELD_RE = re.compile(
    r'(^&inote_([1-7])=)(.*?)(?=^&|\Z)',
    re.MULTILINE | re.DOTALL,
)
_VALID_MARKER_RE = re.compile(r'/S(?=[,/\s]|$)')
_MARKER_LIKE_RE = re.compile(r'(?<![A-Za-z0-9])S[^,\s/]*')
_GRID_DIRECTIVE_RE = re.compile(r'\{([^{}]*)\}')
_VALID_GRID_DIRECTIVE_RE = re.compile(
    r'\{\s*(?:[+-]?\d+|#\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
    r'(?:[eE][+-]?\d+)?)\s*\}',
)
_DIFFICULTY_FIELD_RE = re.compile(
    r'(^&(lv|des|inote)_([1-7])=)(.*?)(?=^&|\Z)',
    re.MULTILINE | re.DOTALL,
)
_DIFFICULTY_FIELD_SCAN_RE = re.compile(
    r'^\ufeff?[ \t]*&(lv|des|inote)_([1-7])=',
    re.MULTILINE,
)
_TRAILING_MARKERS_RE = re.compile(r'((?:/S(?=[/\s]|$)[ \t]*)+)$')
_REFLOW_EPSILON_BEATS = 1e-7
_STRUCTURE_TOLERANCE_SEC = 1e-7


@dataclass
class SweepApplyResult:
    path: Path
    created: bool = False
    stale: bool = False
    warnings: list[str] = field(default_factory=list)


class SweepDifficultyMissingError(ValueError):
    """The requested difficulty is absent from an aggregate maidata file."""


def extract_sweep_difficulty(content: str, difficulty: int) -> str:
    """Return a complete maidata document containing only one chart."""
    inote_fields = 0

    def select_field(match: re.Match) -> str:
        nonlocal inote_fields
        field_difficulty = int(match.group(3))
        if field_difficulty != difficulty:
            return ''
        if match.group(2) == 'inote':
            inote_fields += 1
        return match.group(0)

    result = _DIFFICULTY_FIELD_RE.sub(select_field, content)
    if inote_fields == 0:
        unprojectable_target = any(
            match.group(1) == 'inote'
            and int(match.group(2)) == difficulty
            for match in _DIFFICULTY_FIELD_SCAN_RE.finditer(content)
        )
        if unprojectable_target:
            raise ValueError(
                f'难度 {difficulty} 的 inote 字段存在，但无法安全投影',
            )
        raise SweepDifficultyMissingError(
            f'扫键谱面中没有难度 {difficulty} 的 inote 字段',
        )
    if inote_fields != 1:
        raise ValueError(
            f'扫键谱面应且仅应包含一个难度 {difficulty} 的 inote 字段，'
            f'实际为 {inote_fields} 个',
        )
    residual_difficulties = {
        int(match.group(2))
        for match in _DIFFICULTY_FIELD_SCAN_RE.finditer(result)
        if int(match.group(2)) != difficulty
    }
    if residual_difficulties:
        found = '、'.join(str(value) for value in sorted(residual_difficulties))
        raise ValueError(f'无法安全移除其他难度字段：{found}')
    return result


def _publish_new_sweep_file(path: Path, content: bytes) -> bool:
    """Atomically publish a complete file without replacing concurrent work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A same-directory hard link is atomic and fails if another process
            # has already created the destination, preserving that manual file.
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _inote_groups_with_positions(
    inote: str,
    bpm: float,
) -> tuple[list[str], list[float], list[float], bool]:
    """Return comma groups and their start positions using the real parser."""
    flattened = inote.replace('\r', '').replace('\n', '')
    groups = flattened.split(',')
    # Prefixing a temporary marker also captures empty groups and the group
    # containing the terminating E. Existing manual markers are removed from
    # the probe so every consumed group contributes exactly one marker.
    probe = ','.join([
        *(f'S/{_VALID_MARKER_RE.sub("", group)}' for group in groups),
        'S',
    ])
    notes, timeline, _ = parse_inote(probe, bpm)
    markers = [note for note in notes if note.note_type == NoteType.SWEEP_MARKER]
    reached_sentinel = len(markers) == len(groups) + 1
    if len(markers) > len(groups) + 1:
        raise ValueError('无法建立扫键谱面的逗号时间轴')
    if not reached_sentinel and not markers:
        raise ValueError('无法定位扫键谱面的结束标记 E')
    consumed_count = len(groups) if reached_sentinel else len(markers)
    consumed = groups[:consumed_count]
    markers = markers[:consumed_count]
    times = [float(note.time_sec) for note in markers]
    beats = [time_to_beat(value, timeline) for value in times]
    return consumed, times, beats, not reached_sentinel


def _grid_states(groups: list[str]) -> tuple[list[tuple[int, float | None]],
                                              list[bool]]:
    """Track the comma grid state after each original group."""
    division = 4
    override: float | None = None
    states: list[tuple[int, float | None]] = []
    changes: list[bool] = []
    for group in groups:
        changed = False
        for match in _GRID_DIRECTIVE_RE.finditer(group):
            value = match.group(1).strip()
            if value.startswith('#'):
                try:
                    override = float(value[1:])
                    changed = True
                except ValueError:
                    pass
            else:
                try:
                    division = int(value)
                    override = None
                    changed = True
                except ValueError:
                    pass
        states.append((division, override))
        changes.append(changed)
    return states, changes


def _seconds_directive(seconds: float) -> str:
    return f'{{#{format(seconds, ".15g")}}}'


def _grid_state_directive(state: tuple[int, float | None]) -> str:
    division, override = state
    if override is not None:
        return _seconds_directive(override)
    return f'{{{division}}}'


def _append_grid_directive(group: str, directive: str) -> str:
    """Keep trailing /S tokens at the group end where they remain valid."""
    trailing_markers = _TRAILING_MARKERS_RE.search(group)
    if trailing_markers is None:
        return group + directive
    return (
        group[:trailing_markers.start()]
        + directive
        + trailing_markers.group(1)
    )


def _is_boundary(value: float, boundaries: list[float]) -> bool:
    return any(abs(value - boundary) <= _REFLOW_EPSILON_BEATS
               for boundary in boundaries)


def _expand_meter_map(meter_map: MeterMap, total_beats: float) -> MeterMap:
    """Expand concise sections while preserving explicit reset anchors."""
    expanded: list[MeterMeasure] = []
    sections = meter_map.measures
    for index, section in enumerate(sections):
        end_beat = (
            sections[index + 1].start_beat
            if index + 1 < len(sections)
            else total_beats
        )
        expanded.append(section)
        cursor = section.end_beat
        while cursor < end_beat - _REFLOW_EPSILON_BEATS:
            expanded.append(MeterMeasure(
                cursor,
                section.signature,
                section.confidence,
                section.source,
            ))
            cursor += section.signature.measure_beats
    return MeterMap(expanded, meter_map.default)


def _reflow_inote(inote: str, bpm: float, meter_map: MeterMap,
                  newline: str) -> str:
    """Lay out one inote as one real meter measure per physical line.

    A meter boundary can fall inside a coarse comma step. In that case the
    interval is split with equivalent ``{#seconds}`` empty groups, then the
    original comma grid is restored. Notes, BPM changes and total chart time
    remain unchanged.
    """
    trailing_match = re.search(r'((?:[ \t]*(?:\r\n|\r|\n))+)$', inote)
    trailing = trailing_match.group(1) if trailing_match else ''
    core = inote[:-len(trailing)] if trailing else inote
    flattened = core.replace('\r', '').replace('\n', '')
    all_groups = flattened.split(',')
    groups, times, beats, has_terminal_e = _inote_groups_with_positions(core, bpm)
    if not groups or not times:
        return inote
    ignored_suffix = all_groups[len(groups):]

    total_beats = beats[-1]
    expanded_meter = _expand_meter_map(meter_map, total_beats)
    boundaries = expanded_meter.boundaries(0.0, total_beats)
    states, grid_changes = _grid_states(groups)
    prefixes = [''] * len(groups)
    lines: list[str] = []
    current = ''

    for index in range(len(groups) - 1):
        group = prefixes[index] + groups[index]
        start_beat, end_beat = beats[index], beats[index + 1]
        start_time, end_time = times[index], times[index + 1]
        internal = [
            boundary for boundary in boundaries
            if (boundary > start_beat + _REFLOW_EPSILON_BEATS
                and boundary < end_beat - _REFLOW_EPSILON_BEATS)
        ]

        if internal:
            beat_span = end_beat - start_beat
            time_span = end_time - start_time
            if beat_span <= _REFLOW_EPSILON_BEATS or time_span <= 0:
                raise ValueError(
                    f'小节边界无法切分：beat {internal[0]:g} 位于 '
                    f'{start_beat:g}..{end_beat:g}',
                )
            points = [start_beat, *internal, end_beat]
            durations = [
                time_span * (right - left) / beat_span
                for left, right in zip(points, points[1:])
            ]
            current += _append_grid_directive(
                group, _seconds_directive(durations[0]),
            ) + ','
            lines.append(current)
            current = ''
            for duration in durations[1:-1]:
                lines.append(_seconds_directive(duration) + ',')
            current = _seconds_directive(durations[-1]) + ','

            next_group_is_end = has_terminal_e and index + 1 == len(groups) - 1
            if not next_group_is_end and not grid_changes[index + 1]:
                prefixes[index + 1] = _grid_state_directive(states[index])
        else:
            current += group + ','

        if _is_boundary(end_beat, boundaries):
            lines.append(current)
            current = ''

    last_group = prefixes[-1] + groups[-1]
    if ignored_suffix:
        last_group += ',' + ','.join(ignored_suffix)
    if has_terminal_e:
        if current:
            lines.append(current)
        lines.append(last_group)
    else:
        current += last_group
        if current:
            lines.append(current)
    return newline.join(lines) + trailing


def reflow_sweep_maidata(
    content: str,
    bpm: float,
    meter_maps: Mapping[int, MeterMap],
) -> str:
    """Format every inote with its own difficulty-specific meter map."""
    newline = '\r\n' if '\r\n' in content else '\n'

    def reflow_field(match: re.Match) -> str:
        difficulty = int(match.group(2))
        meter_map = meter_maps.get(difficulty, MeterMap(default='4/4'))
        return match.group(1) + _reflow_inote(
            match.group(3), bpm, meter_map, newline,
        )

    return _INOTE_FIELD_RE.sub(reflow_field, content)


def _maidata_skeleton(content: str) -> tuple[str, list[tuple[int, str]]]:
    """Separate exact non-chart text from semantic inote bodies."""
    fields: list[tuple[int, str]] = []

    def replace_field(match: re.Match) -> str:
        difficulty = int(match.group(2))
        fields.append((difficulty, match.group(3)))
        return f'{match.group(1)}<INOTE_{difficulty}>'

    skeleton = _INOTE_FIELD_RE.sub(replace_field, content)
    return skeleton.replace('\r\n', '\n').replace('\r', '\n'), fields


def _inote_lexical_key(inote: str) -> str:
    """Ignore only edits that meter reflow itself can legitimately create."""
    value = _VALID_MARKER_RE.sub('', inote)
    value = _VALID_GRID_DIRECTIVE_RE.sub('', value)
    value = value.replace(',', '')
    return re.sub(r'\s+', '', value)


def _notes_match(left: list[Note], right: list[Note]) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if (a.note_type != b.note_type or a.button != b.button
                or a.end_button != b.end_button or a.extra != b.extra):
            return False
        if not math.isclose(a.time_sec, b.time_sec, rel_tol=0.0,
                            abs_tol=_STRUCTURE_TOLERANCE_SEC):
            return False
        if not math.isclose(a.duration_sec, b.duration_sec, rel_tol=0.0,
                            abs_tol=_STRUCTURE_TOLERANCE_SEC):
            return False
    return True


def _timelines_match(left: list[tuple[float, float]],
                     right: list[tuple[float, float]]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        left_bpm == right_bpm
        and math.isclose(left_time, right_time, rel_tol=0.0,
                         abs_tol=_STRUCTURE_TOLERANCE_SEC)
        for (left_time, left_bpm), (right_time, right_bpm)
        in zip(left, right)
    )


def sweep_maidata_semantically_equal(left: str, right: str) -> bool:
    """Compare charts while allowing /S and meter-aware timing reflow only."""
    left_skeleton, left_fields = _maidata_skeleton(left)
    right_skeleton, right_fields = _maidata_skeleton(right)
    if left_skeleton != right_skeleton:
        return False
    if [item[0] for item in left_fields] != [item[0] for item in right_fields]:
        return False
    try:
        left_bpm = parse_maidata_content(left).bpm
        right_bpm = parse_maidata_content(right).bpm
        if left_bpm != right_bpm:
            return False
        for (_, left_inote), (_, right_inote) in zip(left_fields, right_fields):
            if _inote_lexical_key(left_inote) != _inote_lexical_key(right_inote):
                return False
            left_clean = _VALID_MARKER_RE.sub('', left_inote)
            right_clean = _VALID_MARKER_RE.sub('', right_inote)
            left_notes, left_timeline, left_init = parse_inote(left_clean, left_bpm)
            right_notes, right_timeline, right_init = parse_inote(right_clean, right_bpm)
            if left_init != right_init:
                return False
            if not _notes_match(left_notes, right_notes):
                return False
            if not _timelines_match(left_timeline, right_timeline):
                return False
            _, left_times, _, left_has_end = _inote_groups_with_positions(
                left_clean, left_bpm,
            )
            _, right_times, _, right_has_end = _inote_groups_with_positions(
                right_clean, right_bpm,
            )
            if left_has_end != right_has_end:
                return False
            left_end = left_times[-1] if left_times else 0.0
            right_end = right_times[-1] if right_times else 0.0
            if not math.isclose(left_end, right_end, rel_tol=0.0,
                                abs_tol=_STRUCTURE_TOLERANCE_SEC):
                return False
    except Exception:
        return False
    return True


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
    difficulty: int,
    sweep_times: Iterable[float] | None = None,
) -> tuple[Path, bool]:
    """Create one difficulty's manual chart; never overwrite existing work."""
    song_root = Path(song_dir)
    source = song_root / 'maidata.txt'
    output = sweep_maidata_path(song_root, difficulty)
    if output.is_file():
        return output, False
    if not source.is_file():
        raise FileNotFoundError(f'Missing maidata.txt: {song_root}')

    source_content = source.read_bytes().decode('utf-8')
    selected: str | None = None
    legacy = legacy_sweep_maidata_path(song_root)
    if legacy.is_file():
        try:
            selected = extract_sweep_difficulty(
                legacy.read_bytes().decode('utf-8'), difficulty,
            )
        except SweepDifficultyMissingError:
            selected = None

    if selected is None:
        selected = extract_sweep_difficulty(source_content, difficulty)
        if sweep_times is not None:
            bpm = parse_maidata_content(selected).bpm
            selected = _seed_sweep_markers(
                selected, bpm, {difficulty: sweep_times},
            )

    parsed = parse_maidata_content(selected)
    if set(parsed.charts) != {difficulty}:
        found = '、'.join(str(value) for value in sorted(parsed.charts)) or '无'
        raise ValueError(
            f'迁移后的扫键文件必须只包含难度 {difficulty}，当前包含：{found}',
        )

    # 按原始 UTF-8 字节发布，避免改变 LF/CRLF 与 UTF-8 BOM。
    created = _publish_new_sweep_file(output, selected.encode('utf-8'))
    return output, created


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
    """Use only this difficulty's manual /S file as sweep-head truth."""
    song_root = Path(song_dir)
    machine_times = [
        float(event['time']) for event in events if event.get('is_sweep_start')
    ]
    path, created = ensure_sweep_maidata(
        song_root, difficulty, machine_times,
    )
    result = SweepApplyResult(path=path, created=created)

    # 人工文件是唯一真值；机器结果只用于文件首次创建时的初始填充。
    for event in events:
        event['is_sweep_start'] = False

    source_content = extract_sweep_difficulty(
        (song_root / 'maidata.txt').read_text(encoding='utf-8'),
        difficulty,
    )
    marker_content = path.read_text(encoding='utf-8')
    result.stale = not sweep_maidata_semantically_equal(
        marker_content, source_content,
    )
    if result.stale:
        result.warnings.append(
            f'{path.name} 除 /S 和等价小节排版外已与 maidata.txt 不一致；'
            '保留人工文件并按现有拍位应用',
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

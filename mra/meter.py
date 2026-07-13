"""拍号时间轴、简洁拍号文件读写与人工编辑模板。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .difficulty import meter_file_path


EPSILON = 1e-6


@dataclass(frozen=True)
class TimeSignature:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("拍号的分子、分母必须为正整数")
        if self.denominator & (self.denominator - 1):
            raise ValueError("拍号分母必须是 2 的幂")

    @property
    def measure_beats(self) -> float:
        """一个小节占多少个四分音符拍。"""
        return self.numerator * 4.0 / self.denominator

    @property
    def label(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    @classmethod
    def parse(cls, value: str | Sequence[int] | "TimeSignature") -> "TimeSignature":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            left, separator, right = value.strip().partition("/")
            if not separator:
                raise ValueError(f"无效拍号: {value!r}")
            return cls(int(left), int(right))
        if len(value) != 2:
            raise ValueError(f"无效拍号: {value!r}")
        return cls(int(value[0]), int(value[1]))


@dataclass(frozen=True)
class MeterMeasure:
    start_beat: float
    signature: TimeSignature
    confidence: float = 1.0
    source: str = "manual"

    @property
    def end_beat(self) -> float:
        return self.start_beat + self.signature.measure_beats


class MeterMap:
    """渲染时使用的逐小节拍号时间轴。"""

    def __init__(self, measures: Iterable[MeterMeasure] | None = None,
                 default: TimeSignature | str = "4/4") -> None:
        self.default = TimeSignature.parse(default)
        ordered = sorted(measures or [], key=lambda measure: measure.start_beat)
        self.measures = self._deduplicate(ordered)
        if not self.measures:
            self.measures = [MeterMeasure(0.0, self.default, 0.0, "fallback")]

    @staticmethod
    def _deduplicate(measures: list[MeterMeasure]) -> list[MeterMeasure]:
        result: list[MeterMeasure] = []
        for measure in measures:
            if result and abs(result[-1].start_beat - measure.start_beat) < EPSILON:
                result[-1] = measure
            else:
                result.append(measure)
        return result

    def measure_at(self, beat: float) -> MeterMeasure:
        result = self.measures[0]
        for measure in self.measures:
            if measure.start_beat > beat + EPSILON:
                break
            result = measure
            if beat < measure.end_beat - EPSILON:
                break
        return result

    def boundaries(self, start: float, end: float) -> list[float]:
        """返回区间内小节线；分析范围外按首末拍号外推。"""
        if end < start:
            start, end = end, start
        values = {round(measure.start_beat, 9) for measure in self.measures
                  if start - EPSILON <= measure.start_beat <= end + EPSILON}
        first = self.measures[0]
        cursor = first.start_beat
        while cursor > start + EPSILON:
            cursor -= first.signature.measure_beats
            if cursor >= start - EPSILON:
                values.add(round(cursor, 9))
        last = self.measures[-1]
        cursor = last.start_beat
        while cursor < end - EPSILON:
            cursor += last.signature.measure_beats
            if cursor <= end + EPSILON:
                values.add(round(cursor, 9))
        return sorted(values)

    def is_boundary(self, beat: float, tolerance: float = 1e-4) -> bool:
        return any(abs(boundary - beat) <= tolerance
                   for boundary in self.boundaries(beat - tolerance, beat + tolerance))

    def ceil_to_boundary(self, beat: float) -> float:
        span = max(12.0, self.measures[-1].signature.measure_beats * 2)
        for boundary in self.boundaries(0.0, beat + span):
            if boundary >= beat - EPSILON:
                return boundary
        return beat

    def add_measures(self, beat: float, count: int) -> float:
        cursor = self.ceil_to_boundary(beat)
        for _ in range(max(0, count)):
            cursor += self.measure_at(cursor + EPSILON).signature.measure_beats
        return cursor

    def signature_sections(self) -> list[dict]:
        """只返回首拍号和真正发生拍号变化的位置。"""
        sections: list[dict] = []
        for measure in self.measures:
            if sections and sections[-1]["signature"] == measure.signature.label:
                continue
            sections.append({
                "start_beat": round(measure.start_beat, 6),
                "signature": measure.signature.label,
                "confidence": round(measure.confidence, 3),
                "source": measure.source,
            })
        return sections

    def to_dict(self, difficulty: int | None = None,
                warnings: Sequence[str] = ()) -> dict:
        """持久化时仅写变化节点；逐小节数据只存在于内存。"""
        result = {
            "version": 2,
            "default": self.default.label,
            "sections": self.signature_sections(),
        }
        if difficulty is not None:
            result["difficulty"] = difficulty
        if warnings:
            result["warnings"] = list(warnings)
        return result

    @classmethod
    def from_dict(cls, data: dict, total_beats: float | None = None) -> "MeterMap":
        default = TimeSignature.parse(data.get("default", "4/4"))
        # 新格式优先读 sections；旧版只有 measures 时仍可兼容。
        raw = data.get("sections") or data.get("measures") or []
        points = [
            MeterMeasure(
                float(item["start_beat"]),
                TimeSignature.parse(item.get(
                    "signature", item.get("time_signature", default.label),
                )),
                float(item.get("confidence", 1.0)),
                str(item.get("source", "manual")),
            )
            for item in raw
        ]
        if not points:
            points = [MeterMeasure(0.0, default, 1.0, "manual")]
        if total_beats is not None:
            points = _expand_sections(points, total_beats)
        return cls(points, default)


def _expand_sections(sections: Sequence[MeterMeasure],
                     total_beats: float) -> list[MeterMeasure]:
    """把简洁变化点展开供渲染；变化点本身始终作为新小节锚点。"""
    result: list[MeterMeasure] = []
    ordered = sorted(sections, key=lambda section: section.start_beat)
    for index, section in enumerate(ordered):
        end = ordered[index + 1].start_beat if index + 1 < len(ordered) else total_beats
        cursor = section.start_beat
        while cursor < end - EPSILON:
            result.append(MeterMeasure(
                cursor, section.signature, section.confidence, section.source,
            ))
            cursor += section.signature.measure_beats
    return result or list(ordered)


def ensure_meter_file(song_dir: str | Path, difficulty: int,
                      total_beats: float | None = None) -> MeterMap:
    """确保拍号文件存在；缺失时只写入默认 4/4，不进行任何检测。"""
    song_root = Path(song_dir)
    output = meter_file_path(song_root, difficulty)
    if output.is_file():
        return MeterMap.from_dict(json.loads(output.read_text(encoding="utf-8")), total_beats)

    meter_map = MeterMap([
        MeterMeasure(0.0, TimeSignature(4, 4), 1.0, "template"),
    ], default="4/4")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(meter_map.to_dict(difficulty), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meter_map


def load_meter_map(song_dir: str | Path, difficulty: int,
                   total_beats: float | None = None) -> MeterMap:
    """渲染端读取变化点并在内存展开；没有结果时保持 4/4。"""
    song_root = Path(song_dir)
    output = meter_file_path(song_root, difficulty)
    if output.is_file():
        return MeterMap.from_dict(json.loads(output.read_text(encoding="utf-8")), total_beats)
    return MeterMap(default="4/4")

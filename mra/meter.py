"""拍号模型、简洁拍号文件读写与 BeatNet+ 纯音频分析。"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .difficulty import meter_analysis_path
from .simai_parser import Chart, time_to_beat


EPSILON = 1e-6
GRID_STEP = 0.5
BEATNET_PLUS_FPS = 50.0
BEATNET_PLUS_BEATS_PER_BAR = (3, 4, 5, 7)
BEATNET_PLUS_MIN_BPM = 55.0
BEATNET_PLUS_MAX_BPM = 215.0
BEATNET_PLUS_METER_CHANGE_PROB = 1e-5
BEATNET_PLUS_WEIGHTS = "generic_weights.pt"
BEATNET_PLUS_REVISION = "bb90eb0a9065b101a4b4c4cb2b2061950266cb4b"
_SESSION_BEATNET_PLUS_PROFILES: dict[tuple[str, int, int], dict] = {}


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


def beat_to_time(beat: float, timeline: Sequence[tuple[float, float]]) -> float:
    """四分音符拍位转秒，作为 ``time_to_beat`` 的分段反函数。"""
    if not timeline:
        return 0.0
    accumulated = 0.0
    for index, (start_time, bpm) in enumerate(timeline):
        end_time = timeline[index + 1][0] if index + 1 < len(timeline) else math.inf
        segment_beats = (end_time - start_time) * bpm / 60.0
        if beat <= accumulated + segment_beats + EPSILON:
            return start_time + max(0.0, beat - accumulated) * 60.0 / max(bpm, EPSILON)
        accumulated += segment_beats
    return timeline[-1][0]


def _audio_fingerprint(audio_path: Path) -> dict:
    stat = audio_path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _import_madmom_downbeats():
    """导入作为解码器的 madmom；兼容 madmom-prebuilt 包名元数据。"""
    try:
        module = importlib.import_module("madmom")
    except importlib.metadata.PackageNotFoundError as exc:
        if exc.name != "madmom":
            raise
        original = importlib.metadata.distribution

        def compatible_distribution(name: str):
            return original("madmom-prebuilt" if name == "madmom" else name)

        importlib.metadata.distribution = compatible_distribution
        try:
            module = importlib.import_module("madmom")
        finally:
            importlib.metadata.distribution = original
    downbeats = importlib.import_module("madmom.features.downbeats")
    beats = importlib.import_module("madmom.features.beats")
    return module, downbeats, beats


def _import_beatnet_plus():
    """导入 BeatNet+；支持安装包或项目 ``.tools`` 下的固定版本。"""
    _import_madmom_downbeats()
    try:
        inference = importlib.import_module("BeatNetPlus.inference")
    except ImportError as first_error:
        source = (Path(__file__).resolve().parents[1] / ".tools" /
                  "BeatNet-Plus" / "src")
        if not source.is_dir():
            raise ImportError(
                "未找到 BeatNet+；请运行 powershell -ExecutionPolicy Bypass "
                "-File tools/setup_beatnet_plus.ps1"
            ) from first_error
        source_text = str(source)
        if source_text not in sys.path:
            sys.path.insert(0, source_text)
        inference = importlib.import_module("BeatNetPlus.inference")
    return inference


def _beatnet_plus_weights_path() -> Path:
    package_source = (Path(__file__).resolve().parents[1] / ".tools" /
                      "BeatNet-Plus" / "src" / "BeatNetPlus" / "models" /
                      BEATNET_PLUS_WEIGHTS)
    if package_source.is_file():
        return package_source
    package = importlib.import_module("BeatNetPlus")
    installed = Path(package.__file__).resolve().parent / "models" / BEATNET_PLUS_WEIGHTS
    if installed.is_file():
        return installed
    raise FileNotFoundError(f"BeatNet+ 缺少模型权重 {BEATNET_PLUS_WEIGHTS}")


def _run_beatnet_plus(audio_path: Path) -> tuple[list[dict], str]:
    """用 BeatNet+ 产生激活，再用扩展拍号状态的 DBN 解码。"""
    inference = _import_beatnet_plus()
    _module, downbeats, beats_module = _import_madmom_downbeats()
    torch = importlib.import_module("torch")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    estimator = inference.BeatNetPlusInference(
        str(_beatnet_plus_weights_path()), mode="online", inference_model="PF",
        device=device,
    )
    audio = estimator._load_audio(str(audio_path))
    features = estimator.proc.process_audio(audio).T
    activations = estimator._get_activations(features)
    if activations.ndim != 2 or activations.shape[1] < 2:
        raise RuntimeError("BeatNet+ 未返回 beat/downbeat 二维激活值")

    # BeatNet+ 输出 50 fps 的 beat/downbeat 概率。DBN 只负责时序解码，
    # 拍号候选扩展到 3/4、4/4、5/4、7/4，不读取谱面音符。
    beat_times = beats_module.DBNBeatTrackingProcessor(
        min_bpm=BEATNET_PLUS_MIN_BPM,
        max_bpm=BEATNET_PLUS_MAX_BPM,
        transition_lambda=60,
        fps=BEATNET_PLUS_FPS,
    )(np.max(activations[:, :2], axis=1))
    if len(beat_times) < 8:
        raise RuntimeError("BeatNet+ 检测到的拍点过少")

    frame_indices = np.clip(
        np.rint(beat_times * BEATNET_PLUS_FPS).astype(int), 0, len(activations) - 1,
    )
    bar_activations = np.column_stack((beat_times, activations[frame_indices, 1]))
    decoded = downbeats.DBNBarTrackingProcessor(
        beats_per_bar=BEATNET_PLUS_BEATS_PER_BAR,
        meter_change_prob=BEATNET_PLUS_METER_CHANGE_PROB,
    )(bar_activations)
    downbeat_indices = np.flatnonzero(decoded[:, 1].astype(int) == 1)
    if len(downbeat_indices) < 2:
        raise RuntimeError("BeatNet+ 未检测到完整小节")

    bars: list[tuple[int, int]] = []
    for left, right in zip(downbeat_indices, downbeat_indices[1:]):
        beats_per_bar = int(right - left)
        if beats_per_bar in BEATNET_PLUS_BEATS_PER_BAR:
            bars.append((int(left), beats_per_bar))
    if not bars:
        raise RuntimeError("BeatNet+ 未得到可用拍号")

    runs: list[list[tuple[int, int]]] = []
    run_start = 0
    for index in range(1, len(bars) + 1):
        changed = index == len(bars) or bars[index][1] != bars[run_start][1]
        if not changed:
            continue
        runs.append(bars[run_start:index])
        run_start = index

    # DBN 偶尔会在稳定段中插入一个单小节异拍。两侧拍号相同的单小节尖峰
    # 视为解码抖动，避免 meter.json 出现无法人工理解的往返变化。
    run_index = 1
    while run_index + 1 < len(runs):
        if (len(runs[run_index]) == 1
                and runs[run_index - 1][0][1] == runs[run_index + 1][0][1]):
            runs[run_index - 1].extend(runs[run_index])
            runs[run_index - 1].extend(runs[run_index + 1])
            del runs[run_index:run_index + 2]
            continue
        run_index += 1

    sections: list[dict] = []
    for run in runs:
        beat_index, beats_per_bar = run[0]
        starts = np.asarray([item[0] for item in run], dtype=int)
        downbeat_frames = frame_indices[starts]
        class_margin = activations[downbeat_frames, 1] - activations[downbeat_frames, 0]
        confidence = float(np.clip(0.5 + 0.5 * np.mean(class_margin), 0.0, 1.0))
        sections.append({
            "time": round(float(decoded[beat_index, 0]), 6),
            "beats_per_bar": beats_per_bar,
            "confidence": round(confidence, 3),
        })
    return sections, BEATNET_PLUS_REVISION[:7]


def _beatnet_plus_cache_path(song_dir: Path) -> Path:
    return song_dir / "outputs" / "_shared" / "meter" / "beatnet-plus.json"


def _beatnet_plus_profile(audio_path: Path, cache_path: Path,
                          force: bool = False) -> tuple[dict | None, str | None]:
    """读取或生成同一首歌跨难度共享的纯音频分析缓存。"""
    if not audio_path.is_file():
        return None, "缺少 track.mp3，无法使用 BeatNet+ 分析拍号"
    fingerprint = _audio_fingerprint(audio_path)
    session_key = (str(audio_path.resolve()), fingerprint["size"], fingerprint["mtime_ns"])
    if session_key in _SESSION_BEATNET_PLUS_PROFILES:
        return _SESSION_BEATNET_PLUS_PROFILES[session_key], None
    if cache_path.is_file() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (cached.get("audio") == fingerprint
                    and cached.get("backend") == "beatnet-plus"
                    and cached.get("weights") == BEATNET_PLUS_WEIGHTS):
                _SESSION_BEATNET_PLUS_PROFILES[session_key] = cached
                return cached, None
        except (OSError, ValueError, TypeError):
            pass
    try:
        sections, package_version = _run_beatnet_plus(audio_path)
    except ImportError:
        return None, ("未安装 BeatNet+；请运行 powershell -ExecutionPolicy Bypass "
                      "-File tools/setup_beatnet_plus.ps1")
    except Exception as exc:
        return None, f"BeatNet+ 纯音频拍号分析失败: {type(exc).__name__}: {exc}"
    cached = {
        "version": 1,
        "backend": "beatnet-plus",
        "package_version": package_version,
        "weights": BEATNET_PLUS_WEIGHTS,
        "fps": BEATNET_PLUS_FPS,
        "beats_per_bar": list(BEATNET_PLUS_BEATS_PER_BAR),
        "confidence": "BeatNet+ downbeat-vs-beat class margin; not calibrated",
        "audio": fingerprint,
        "sections": sections,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cached, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    _SESSION_BEATNET_PLUS_PROFILES[session_key] = cached
    return cached, None


def _profile_to_meter_map(profile: dict, chart: Chart, total_beats: float,
                          first_offset: float) -> MeterMap:
    sections: list[MeterMeasure] = []
    for index, item in enumerate(profile.get("sections", [])):
        audio_time = float(item["time"])
        chart_time = max(0.0, audio_time - first_offset)
        start_beat = round(time_to_beat(chart_time, chart.bpm_timeline) / GRID_STEP) * GRID_STEP
        if index == 0:
            # 文件从曲首即可读；首个检测下拍只用于确定拍号，不制造前置空段。
            start_beat = 0.0
        if start_beat > total_beats + EPSILON:
            continue
        signature = TimeSignature(int(item["beats_per_bar"]), 4)
        measure = MeterMeasure(
            start_beat,
            signature,
            float(item.get("confidence", 0.5)),
            str(profile.get("backend", "beatnet-plus")),
        )
        if sections and sections[-1].signature == measure.signature:
            continue
        sections.append(measure)
    if not sections:
        return MeterMap(default="4/4")
    return MeterMap(_expand_sections(sections, total_beats), sections[0].signature)


def _manual_config(song_dir: Path, difficulty: int) -> dict | None:
    path = song_dir / "meter.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    difficulties = data.get("difficulties")
    if isinstance(difficulties, dict):
        selected = difficulties.get(str(difficulty))
        if selected is None:
            return None
        merged = {key: value for key, value in data.items() if key != "difficulties"}
        merged.update(selected)
        return merged
    return data


def analyze_chart_meter(song_dir: str | Path, difficulty: int, chart: Chart,
                        total_beats: float, first_offset: float = 0.0,
                        force: bool = False) -> MeterMap:
    """读取人工配置或执行 BeatNet+ 纯音频分析并写出简洁变化点。"""
    song_root = Path(song_dir)
    output = meter_analysis_path(song_root, difficulty)
    manual = _manual_config(song_root, difficulty)
    warnings: list[str] = []
    if manual is not None:
        meter_map = MeterMap.from_dict(manual, total_beats)
    elif output.is_file() and not force:
        return MeterMap.from_dict(json.loads(output.read_text(encoding="utf-8")), total_beats)
    else:
        profile, warning = _beatnet_plus_profile(
            song_root / "track.mp3", _beatnet_plus_cache_path(song_root), force=force,
        )
        if warning:
            warnings.append(warning)
        meter_map = (_profile_to_meter_map(profile, chart, total_beats, first_offset)
                     if profile is not None else MeterMap(default="4/4"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(meter_map.to_dict(difficulty, warnings), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meter_map


def load_meter_map(song_dir: str | Path, difficulty: int,
                   total_beats: float | None = None) -> MeterMap:
    """渲染端读取变化点并在内存展开；没有结果时保持 4/4。"""
    song_root = Path(song_dir)
    manual = _manual_config(song_root, difficulty)
    if manual is not None:
        return MeterMap.from_dict(manual, total_beats)
    output = meter_analysis_path(song_root, difficulty)
    if output.is_file():
        return MeterMap.from_dict(json.loads(output.read_text(encoding="utf-8")), total_beats)
    return MeterMap(default="4/4")

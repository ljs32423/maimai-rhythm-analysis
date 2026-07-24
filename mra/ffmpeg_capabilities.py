"""FFmpeg discovery, encoder probing, and hardware-to-software fallback."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import load_config
from .song_library import PROJECT_ROOT


ENCODER_ORDER = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
ENCODER_NAMES = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel Quick Sync",
    "h264_amf": "AMD AMF",
    "libx264": "CPU Software",
}
_CAPABILITY_CACHE: dict[str, "FFmpegCapabilities"] = {}


@dataclass(frozen=True)
class EncoderProbe:
    codec: str
    name: str
    available: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FFmpegCapabilities:
    ffmpeg: str
    ffprobe: str | None
    version: str
    encoders: tuple[EncoderProbe, ...]
    selected: str

    def to_dict(self) -> dict:
        return {
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "version": self.version,
            "encoders": [encoder.to_dict() for encoder in self.encoders],
            "selected": self.selected,
            "selected_name": ENCODER_NAMES.get(self.selected, self.selected),
        }


def _tool_roots() -> list[Path]:
    roots = [
        PROJECT_ROOT.parent / "required-programs" / ".tools",
        PROJECT_ROOT / ".tools",
    ]
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _versioned_candidates(root: Path, family: str, executable: str) -> Iterable[Path]:
    base = root / family
    if not base.is_dir():
        return ()
    return (
        path / executable
        for path in sorted(base.iterdir(), reverse=True)
        if path.is_dir()
    )


def find_binary(tool: str, extra_roots: Sequence[str | Path] = ()) -> str | None:
    env_name = f"MRA_{tool.upper()}"
    override = os.environ.get(env_name)
    if override and Path(override).is_file():
        return str(Path(override).resolve())

    executable = f"{tool}.exe" if os.name == "nt" else tool
    roots = [Path(root) for root in extra_roots] + _tool_roots()
    for root in roots:
        direct = root / executable
        if direct.is_file():
            return str(direct.resolve())
        for family in ("ffmpeg", "ffprobe"):
            for candidate in _versioned_candidates(root, family, executable):
                if candidate.is_file():
                    return str(candidate.resolve())
        for family in ("majdataviewx", "majdata"):
            base = root / family
            if not base.is_dir():
                continue
            for version in sorted(base.iterdir(), reverse=True):
                candidates = (
                    version / executable,
                    version / "MajdataView_Data" / "StreamingAssets" / executable,
                    version / "Majdata" / "MajdataView_Data" / "StreamingAssets" / executable,
                )
                for candidate in candidates:
                    if candidate.is_file():
                        return str(candidate.resolve())
    return shutil.which(executable) or shutil.which(tool)


def require_binary(tool: str, extra_roots: Sequence[str | Path] = ()) -> str:
    result = find_binary(tool, extra_roots)
    if result:
        return result
    raise FileNotFoundError(f"未找到 {tool}；请使用完整发行包或设置 MRA_{tool.upper()}")


def _short_reason(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "试编码失败"


def _probe_encoder(ffmpeg: str, codec: str, timeout: float = 10) -> EncoderProbe:
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1:r=30",
        *encoder_args(codec, "high", purpose="recording"),
        "-frames:v", "3", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EncoderProbe(codec, ENCODER_NAMES[codec], False, str(exc))
    return EncoderProbe(
        codec,
        ENCODER_NAMES[codec],
        result.returncode == 0,
        "" if result.returncode == 0 else _short_reason(result.stderr),
    )


def _ffmpeg_version(ffmpeg: str) -> str:
    try:
        result = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    first = result.stdout.splitlines()[0] if result.stdout else ""
    match = re.search(r"ffmpeg version\s+([^\s]+)", first)
    return match.group(1) if match else "unknown"


def detect_capabilities(
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    preference: str | None = None,
    *,
    refresh: bool = False,
) -> FFmpegCapabilities:
    resolved_ffmpeg = str(Path(ffmpeg or require_binary("ffmpeg")).resolve())
    resolved_ffprobe = ffprobe or find_binary("ffprobe")
    configured = preference or load_config()[0].get("encoder", "auto")
    cache_key = f"{resolved_ffmpeg}|{configured}"
    if not refresh and cache_key in _CAPABILITY_CACHE:
        return _CAPABILITY_CACHE[cache_key]

    probes = tuple(_probe_encoder(resolved_ffmpeg, codec) for codec in ENCODER_ORDER)
    available = {probe.codec for probe in probes if probe.available}
    if configured != "auto" and configured in available:
        selected = configured
    else:
        selected = next((codec for codec in ENCODER_ORDER if codec in available), "libx264")
    capabilities = FFmpegCapabilities(
        ffmpeg=resolved_ffmpeg,
        ffprobe=str(Path(resolved_ffprobe).resolve()) if resolved_ffprobe else None,
        version=_ffmpeg_version(resolved_ffmpeg),
        encoders=probes,
        selected=selected,
    )
    _CAPABILITY_CACHE[cache_key] = capabilities
    return capabilities


def encoder_args(codec: str, quality: str = "high", purpose: str = "transcode") -> list[str]:
    quality_values = {
        "balanced": {"cq": "22", "qsv": "22", "crf": "21"},
        "high": {"cq": "18", "qsv": "18", "crf": "18"},
        "maximum": {"cq": "15", "qsv": "15", "crf": "15"},
    }
    values = quality_values.get(quality, quality_values["high"])
    if codec == "h264_nvenc":
        args = [
            "-c:v", codec, "-preset", "p4", "-tune", "hq",
            "-rc:v", "vbr", "-cq:v", values["cq"], "-b:v", "0",
        ]
    elif codec == "h264_qsv":
        args = [
            "-c:v", codec, "-preset", "medium",
            "-global_quality", values["qsv"],
        ]
    elif codec == "h264_amf":
        base = int(values["cq"])
        args = [
            "-c:v", codec, "-quality", "quality", "-rc", "cqp",
            "-qp_i", str(base), "-qp_p", str(base + 2), "-qp_b", str(base + 4),
        ]
    else:
        preset = "ultrafast" if purpose == "recording" else "veryfast"
        args = ["-c:v", "libx264", "-preset", preset, "-crf", values["crf"]]
        if purpose == "recording":
            args += ["-tune", "zerolatency"]
    return args


CommandFactory = Callable[[str], list[str]]


def run_with_fallback(
    command_factory: CommandFactory,
    *,
    ffmpeg: str | None = None,
    preference: str | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
) -> tuple[subprocess.CompletedProcess, str]:
    capabilities = detect_capabilities(ffmpeg=ffmpeg, preference=preference)
    attempts = [capabilities.selected]
    if capabilities.selected != "libx264":
        attempts.append("libx264")
    errors: list[str] = []
    for codec in attempts:
        command = command_factory(codec)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{codec}: {exc}")
            continue
        if result.returncode == 0:
            return result, codec
        errors.append(f"{codec}: {_short_reason(result.stderr)}")
    if "result" not in locals():
        raise RuntimeError("; ".join(errors))
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr="; ".join(errors),
    )


def clear_capability_cache() -> None:
    _CAPABILITY_CACHE.clear()

"""Application configuration shared by the CLI, web app, and release launcher."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .song_library import PROJECT_ROOT


CONFIG_VERSION = 1
DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "songs_root": "songs",
    "encoder": "auto",
    "recording": {
        "width": 2560,
        "height": 1440,
        "fps": 60,
        "quality": "high",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 0,
        "open_browser": True,
    },
}
VALID_ENCODERS = {"auto", "h264_nvenc", "h264_qsv", "h264_amf", "libx264"}
VALID_QUALITIES = {"balanced", "high", "maximum"}


class ConfigError(ValueError):
    """Raised when a configuration value cannot be accepted."""


def config_path() -> Path:
    override = os.environ.get("MRA_CONFIG")
    return Path(override).expanduser().resolve() if override else PROJECT_ROOT / "config.json"


def _merge_dict(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")
    merged = _merge_dict(DEFAULT_CONFIG, data)

    songs_root = merged.get("songs_root")
    if not isinstance(songs_root, str) or not songs_root.strip():
        raise ConfigError("songs_root 必须是非空路径")
    merged["songs_root"] = songs_root.strip()

    encoder = str(merged.get("encoder", "auto"))
    if encoder not in VALID_ENCODERS:
        raise ConfigError(f"不支持的编码器: {encoder}")
    merged["encoder"] = encoder

    recording = merged.get("recording")
    if not isinstance(recording, dict):
        raise ConfigError("recording 必须是对象")
    for key, lower, upper in (
        ("width", 640, 7680),
        ("height", 360, 4320),
        ("fps", 24, 120),
    ):
        try:
            value = int(recording[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"recording.{key} 必须是整数") from exc
        if not lower <= value <= upper:
            raise ConfigError(f"recording.{key} 必须在 {lower} 到 {upper} 之间")
        recording[key] = value
    quality = str(recording.get("quality", "high"))
    if quality not in VALID_QUALITIES:
        raise ConfigError(f"不支持的画质档位: {quality}")
    recording["quality"] = quality

    web = merged.get("web")
    if not isinstance(web, dict):
        raise ConfigError("web 必须是对象")
    host = str(web.get("host", "127.0.0.1")).strip()
    if host not in {"127.0.0.1", "localhost"}:
        raise ConfigError("本地 Web 应用只允许绑定 127.0.0.1 或 localhost")
    web["host"] = host
    try:
        port = int(web.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("web.port 必须是整数") from exc
    if not 0 <= port <= 65535:
        raise ConfigError("web.port 必须在 0 到 65535 之间")
    web["port"] = port
    web["open_browser"] = bool(web.get("open_browser", True))
    merged["version"] = CONFIG_VERSION
    return merged


def load_config(path: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    target = Path(path) if path else config_path()
    if not target.is_file():
        return deepcopy(DEFAULT_CONFIG), []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return validate_config(raw), []
    except (OSError, json.JSONDecodeError, ConfigError) as exc:
        return deepcopy(DEFAULT_CONFIG), [f"无法读取 {target.name}，已使用默认配置: {exc}"]


def save_config(data: dict[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else config_path()
    validated = validate_config(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        # 清理失败（杀毒软件锁定、沙箱拦截删除等）不影响保存结果
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return validated


def resolve_songs_root(config: dict[str, Any] | None = None) -> Path:
    active = config if config is not None else load_config()[0]
    path = Path(active["songs_root"]).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()

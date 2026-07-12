#!/usr/bin/env python3
"""
MajdataView 谱面预览视频录制
==============================
通过 MajdataView (官方 simai 渲染器) 的 HTTP API 自动录制谱面预览视频。
流程: 安装 MajdataView → 转换 maidata.txt 为 JSON → 通过 HTTP 触发录制 → 等待输出。
录制完成后生成 outputs/{难度}/video/preview.mp4。
"""

import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import warnings
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.io.wavfile import WavFileWarning
from scipy.signal import resample_poly

from .difficulty import (default_target_difficulties, difficulty_name,
                         legacy_difficulty_path, preview_video_path)
from .simai_parser import parse_maidata
from .song_library import PROJECT_ROOT, discover_song_folders


ROOT = PROJECT_ROOT
MAJDATA_VERSION = "4.3.1"
MAJDATA_ARCHIVE_URL = (
    "https://github.com/LingFeng-bbben/MajdataView/releases/download/"
    "v4.3.1/Majdata-3b329da-5aad37e.7z"
)
LOCAL_TOOLS_ROOT = ROOT / ".tools"
SIBLING_TOOLS_ROOT = ROOT.parent / "required-programs" / ".tools"
BRIDGE_PROJECT = ROOT / "tools" / "src" / "majdata_bridge" / "MajdataBridge.csproj"
HTTP_URL = "http://localhost:8013/"
MAJDATA_X_WS_HOST = "127.0.0.1"
MAJDATA_X_WS_PORT = 8083
MAJDATA_X_WS_PATH = "/majdata"
DEFAULT_TAP_SPEED = 7.5
DEFAULT_TOUCH_SPEED = 7.5
DEFAULT_BACKGROUND_COVER = 0.5
ENABLE_PV_PLAYBACK = False

# 录制分辨率选项（宽, 高），直接修改下面两个常量即可切换：
# 1920, 1080  -> Full HD，速度最快
# 2560, 1440  -> 2K，画质与速度平衡
# 3840, 2160  -> 4K，最高画质（当前默认）
RECORD_WIDTH = 2560
RECORD_HEIGHT = 1440
RECORD_FPS = 60

# 录制后输出给前端的实际预览视频裁剪。
# MajdataViewX 的 16:9 画面左右有参数区；取中心正方形能完整保留圆形游戏界面。
ENABLE_OUTPUT_CROP = True

# MajdataEdit 的官方录制流程默认会把键音混入 out.wav。HTTP 录制接口没有
# 独立的键音参数，因此这里显式生成同类音轨；改为 False 可录制纯 BGM。
ENABLE_KEY_SOUNDS = True
KEY_SOUND_VOLUME = 0.7
RECORDING_LEAD_IN_SECONDS = 5.0

RECORDER_FFMPEG_ARGUMENTS = (
    f'-hide_banner -y -f rawvideo -vcodec rawvideo -pix_fmt rgba '
    f'-s "{{0}}x{{1}}" -r {RECORD_FPS} -i \\\\.\\pipe\\majdataRec -i "{{2}}" '
    f'-vf "vflip" -c:v libx264 -preset ultrafast -tune zerolatency '
    f'-crf 18 -pix_fmt yuv420p -r {RECORD_FPS} -fps_mode cfr -t "{{4:0.0000}}" '
    f'-b:a 320k -c:a aac -movflags +faststart "{{3}}"'
)


def tools_roots() -> list[Path]:
    roots: list[Path] = []
    for candidate in (SIBLING_TOOLS_ROOT, LOCAL_TOOLS_ROOT):
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def default_majdata_home() -> Path:
    for tools_root in tools_roots():
        candidate = tools_root / "majdata" / MAJDATA_VERSION / "Majdata"
        if (candidate / "MajdataView.exe").exists():
            return candidate
    return LOCAL_TOOLS_ROOT / "majdata" / MAJDATA_VERSION / "Majdata"


def bridge_candidates() -> list[Path]:
    candidates: list[Path] = []
    for tools_root in tools_roots():
        output = tools_root / "majdata_bridge"
        for candidate in (output / "MajdataBridge.exe", output / "MajdataBridge.dll"):
            if candidate.resolve() not in [p.resolve() for p in candidates]:
                candidates.append(candidate)
    return candidates


def find_executable(name: str, majdata_home: Path | None = None):
    if majdata_home:
        bundled = majdata_home / name
        if bundled.exists():
            return str(bundled)
        bundled = majdata_home / "MajdataView_Data" / "StreamingAssets" / name
        if bundled.exists():
            return str(bundled)
    return shutil.which(name)


def configure_recorder(majdata_home: Path) -> Path:
    """Keep MajdataView's bundled FFmpeg recorder at the project settings."""
    arguments_path = (
        majdata_home / "MajdataView_Data" / "StreamingAssets" / "ffarguments.txt"
    )
    if not arguments_path.exists():
        raise FileNotFoundError(f"缺少 MajdataView 录制配置: {arguments_path}")
    current = arguments_path.read_text(encoding="utf-8").strip()
    if current != RECORDER_FFMPEG_ARGUMENTS:
        arguments_path.write_text(RECORDER_FFMPEG_ARGUMENTS + "\n", encoding="utf-8")
    return arguments_path


def install_majdata_view() -> Path:
    env_home = os.environ.get("MAJDATA_HOME")
    if env_home:
        home = Path(env_home).expanduser().resolve()
        if not (home / "MajdataView.exe").exists():
            raise FileNotFoundError(f"MAJDATA_HOME 中没有 MajdataView.exe: {home}")
        return home

    detected_home = default_majdata_home()
    if (detected_home / "MajdataView.exe").exists():
        return detected_home

    archive = LOCAL_TOOLS_ROOT / "downloads" / f"Majdata-{MAJDATA_VERSION}.7z"
    install_root = (LOCAL_TOOLS_ROOT / "majdata" / MAJDATA_VERSION).parent
    archive.parent.mkdir(parents=True, exist_ok=True)
    install_root.mkdir(parents=True, exist_ok=True)
    print(f"  下载 MajdataView v{MAJDATA_VERSION}...")
    urllib.request.urlretrieve(MAJDATA_ARCHIVE_URL, archive)

    tar = shutil.which("tar")
    if not tar:
        raise RuntimeError("未找到 tar，无法解压 MajdataView 的 7z 安装包")
    subprocess.run([tar, "-xf", str(archive), "-C", str(install_root)], check=True)
    local_home = LOCAL_TOOLS_ROOT / "majdata" / MAJDATA_VERSION / "Majdata"
    if not (local_home / "MajdataView.exe").exists():
        raise RuntimeError("MajdataView 解压完成，但未找到 MajdataView.exe")
    return local_home


def build_bridge() -> Path:
    sources = list(BRIDGE_PROJECT.parent.glob("*.cs")) + [BRIDGE_PROJECT]
    for candidate in bridge_candidates():
        if candidate.exists() and all(candidate.stat().st_mtime >= p.stat().st_mtime for p in sources):
            return candidate
    if not shutil.which("dotnet"):
        raise RuntimeError("未找到 .NET SDK，无法调用 MajdataEdit 的官方谱面解析器")
    bridge_output = LOCAL_TOOLS_ROOT / "majdata_bridge"
    bridge_output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "dotnet", "publish", str(BRIDGE_PROJECT),
            "-c", "Release",
            "-r", "win-x64",
            "--self-contained", "true",
            "-p:PublishSingleFile=true",
            "-p:PublishTrimmed=false",
            "-o", str(bridge_output),
        ],
        cwd=ROOT,
        check=True,
    )
    for candidate in (bridge_output / "MajdataBridge.exe", bridge_output / "MajdataBridge.dll"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"未生成 MajdataBridge: {bridge_output}")


def convert_maidata(majdata_home: Path, song_dir: Path, difficulty: int,
                    output_path: Path | None = None) -> Path:
    bridge = build_bridge()
    output = output_path or song_dir / "majdata.json"
    command = (
        [str(bridge), str(majdata_home), str(song_dir / "maidata.txt"), str(difficulty), str(output)]
        if bridge.suffix.lower() == ".exe" else
        ["dotnet", str(bridge), str(majdata_home), str(song_dir / "maidata.txt"),
         str(difficulty), str(output)]
    )
    subprocess.run(command, cwd=ROOT, check=True)
    return output


def prepare_audio(majdata_home: Path, song_dir: Path,
                  work_dir: Path | None = None,
                  chart_json: Path | None = None) -> tuple[Path, bool]:
    output = (work_dir or song_dir) / "out.wav"
    if output.exists():
        return output, False
    track = song_dir / "track.mp3"
    if not track.exists():
        raise FileNotFoundError(f"缺少音频: {track}")
    ffmpeg = find_executable("ffmpeg.exe", majdata_home) or find_executable("ffmpeg", majdata_home)
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(track),
         "-af", f"adelay={int(RECORDING_LEAD_IN_SECONDS * 1000)}:all=1",
         "-ar", "44100", str(output)],
        cwd=song_dir,
        check=True,
    )
    if ENABLE_KEY_SOUNDS:
        if chart_json is None:
            raise ValueError("启用键音时必须提供 majdata.json")
        mix_key_sounds(majdata_home, chart_json, output)
    return output, True


def build_key_sound_events(chart_data: dict) -> tuple[dict[float, set[str]], list[tuple[float, float]]]:
    """Translate official Majdata note JSON into its recording SFX timeline."""
    events: dict[float, set[str]] = {}
    touch_holds: list[tuple[float, float]] = []

    def add(at: float, *sounds: str):
        if at < 0:
            return
        events.setdefault(round(float(at), 6), set()).update(sounds)

    def add_head(at: float, note: dict):
        add(at, "answer")
        if note.get("isBreak"):
            add(at, "break", "judge_break")
        if note.get("isEx"):
            add(at, "judge_ex")
        if not note.get("isBreak") and not note.get("isEx"):
            add(at, "judge")

    for group in chart_data.get("timingList", []):
        at = float(group.get("time", 0.0))
        for note in group.get("noteList", []):
            note_type = int(note.get("noteType", -1))
            if note_type in (0, 2):  # Tap / Hold
                add_head(at, note)
                if note_type == 2 and float(note.get("holdTime", 0.0)) > 0:
                    release = at + float(note["holdTime"])
                    add(release, "answer")
                    if not note.get("isBreak") and not note.get("isEx"):
                        add(release, "judge")
            elif note_type == 1:  # Slide
                if not note.get("isSlideNoHead"):
                    add_head(at, note)
                slide_start = float(note.get("slideStartTime", at))
                add(slide_start, "break_slide_start" if note.get("isSlideBreak") else "slide")
                if note.get("isSlideBreak"):
                    slide_end = slide_start + float(note.get("slideTime", 0.0))
                    add(slide_end, "break_slide", "judge_break_slide")
            elif note_type == 3:  # Touch
                add(at, "answer", "touch")
                if note.get("isHanabi"):
                    add(at, "hanabi")
            elif note_type == 4:  # Touch Hold
                duration = max(0.0, float(note.get("holdTime", 0.0)))
                add(at, "answer", "touch")
                if duration:
                    touch_holds.append((at, at + duration))
                    add(at + duration, "answer")
                    if note.get("isHanabi"):
                        add(at + duration, "hanabi")

    return events, touch_holds


def _audio_as_float(data: np.ndarray) -> np.ndarray:
    if np.issubdtype(data.dtype, np.floating):
        result = data.astype(np.float32, copy=False)
    else:
        info = np.iinfo(data.dtype)
        result = data.astype(np.float32) / max(abs(info.min), info.max)
    if result.ndim == 1:
        result = np.column_stack((result, result))
    elif result.shape[1] > 2:
        result = result[:, :2]
    return result


def mix_key_sounds(majdata_home: Path, chart_json: Path, output: Path) -> None:
    """Mix MajdataEdit's bundled SFX into the prepared recording WAV."""
    sample_rate, base_data = wavfile.read(output)
    mixed = _audio_as_float(base_data).copy()
    chart_data = json.loads(chart_json.read_text(encoding="utf-8"))
    events, touch_holds = build_key_sound_events(chart_data)
    sfx_root = majdata_home / "SFX"
    cache: dict[str, np.ndarray] = {}

    def load_sfx(name: str) -> np.ndarray:
        if name in cache:
            return cache[name]
        path = sfx_root / f"{name}.wav"
        if not path.exists():
            raise FileNotFoundError(f"缺少 Majdata 键音: {path}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", WavFileWarning)
            rate, data = wavfile.read(path)
        sound = _audio_as_float(data)
        if rate != sample_rate:
            divisor = math.gcd(rate, sample_rate)
            sound = resample_poly(sound, sample_rate // divisor, rate // divisor, axis=0)
        cache[name] = sound.astype(np.float32, copy=False)
        return cache[name]

    def overlay(sound: np.ndarray, at: float, volume: float = KEY_SOUND_VOLUME):
        start = round(at * sample_rate)
        if start >= len(mixed):
            return
        end = min(start + len(sound), len(mixed))
        mixed[start:end] += sound[:end - start] * volume

    # Official recording plays this cue inside the fixed five-second lead-in.
    overlay(load_sfx("track_start"), 0.0)
    for at, names in sorted(events.items()):
        for name in sorted(names):
            volume = KEY_SOUND_VOLUME * (0.75 if name == "break" else 1.0)
            overlay(load_sfx(name), RECORDING_LEAD_IN_SECONDS + at, volume)
    for start, end in touch_holds:
        riser = load_sfx("touchHold_riser")
        length = max(0, round((end - start) * sample_rate))
        overlay(riser[:length], RECORDING_LEAD_IN_SECONDS + start)

    wavfile.write(output, sample_rate, (np.clip(mixed, -1.0, 1.0) * 32767).astype(np.int16))


def _find_pv(song_dir: Path) -> Path | None:
    for name in ("pv.mp4", "mv.mp4", "bg.mp4"):
        path = song_dir / name
        if path.exists():
            return path
    return None


def prepare_recording_assets(ffmpeg: str, song_dir: Path, work_dir: Path) -> Path | None:
    """Create a timestamp-safe PV for Unity without touching the source video."""
    for stem in ("Cover", "bg"):
        for extension in (".png", ".jpg", ".jpeg"):
            source = song_dir / f"{stem}{extension}"
            if source.exists():
                shutil.copy2(source, work_dir / source.name)
                break

    if not ENABLE_PV_PLAYBACK:
        return None

    source_pv = _find_pv(song_dir)
    if source_pv is None:
        return None

    output = work_dir / "pv.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-i", str(source_pv),
            "-an", "-vf", f"setpts=PTS-STARTPTS,fps={RECORD_FPS}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-profile:v", "baseline", "-level:v", "4.2",
            "-pix_fmt", "yuv420p", "-g", str(RECORD_FPS * 2),
            "-video_track_timescale", str(RECORD_FPS * 1000),
            "-movflags", "+faststart", str(output),
        ],
        cwd=work_dir,
        check=True,
    )
    return output


def http_ready() -> bool:
    try:
        with urllib.request.urlopen(HTTP_URL, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def majdatax_ws_ready() -> bool:
    try:
        with socket.create_connection((MAJDATA_X_WS_HOST, MAJDATA_X_WS_PORT), timeout=1):
            return True
    except OSError:
        return False


class SimpleWebSocket:
    """Small RFC 6455 text client for MajdataViewX's local control socket."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.path = path
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._recv_http_response()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if " 101 " not in response or accept not in response:
            raise RuntimeError(f"MajdataViewX WebSocket 握手失败: {response.splitlines()[0] if response else '无响应'}")

    def _recv_http_response(self) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("iso-8859-1", errors="replace")

    def _recv_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("MajdataViewX WebSocket 连接已关闭")
            data += chunk
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(payload)))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_text(self, timeout: float = 10.0) -> str:
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            while True:
                first, second = self._recv_exact(2)
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(length) if length else b""
                if masked:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode == 0x1:
                    return payload.decode("utf-8", errors="replace")
                if opcode == 0x8:
                    raise RuntimeError("MajdataViewX WebSocket 已关闭")
                if opcode == 0x9:
                    self.sock.sendall(b"\x8a\x00")
        finally:
            self.sock.settimeout(old_timeout)

    def close(self) -> None:
        try:
            self.sock.sendall(b"\x88\x00")
        except OSError:
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def majdatax_request(ws: SimpleWebSocket, request_type: int, request_data: dict | None,
                     expected: set[int] | None = None, timeout: float = 30.0) -> dict | None:
    ws.send_text(json.dumps({
        "requestType": request_type,
        "requestData": request_data,
    }, ensure_ascii=False))
    if expected is None:
        return None
    deadline = time.monotonic() + timeout
    last_response: dict | None = None
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            response = json.loads(ws.recv_text(timeout=remaining))
        except socket.timeout:
            break
        last_response = response
        response_type = int(response.get("responseType", -1))
        if response_type in expected:
            return response
        if response_type == 400:
            raise RuntimeError(f"MajdataViewX 返回错误: {response.get('responseData')}")
    raise TimeoutError(f"MajdataViewX 等待响应超时，最后响应: {last_response}")


def post_record(json_path: Path):
    # MajdataView's recording clock always starts after a fixed five-second lead-in.
    dotnet_ticks = int((time.time() + 62135596800 + 5) * 10_000_000)
    payload = {
        "audioSpeed": 1.0,
        "backgroundCover": DEFAULT_BACKGROUND_COVER,
        "comboStatusType": 0,
        "editorPlayMethod": 0,
        "control": 5,
        "jsonPath": str(json_path),
        "noteSpeed": DEFAULT_TAP_SPEED,
        "startAt": dotnet_ticks,
        "startTime": 0.0,
        "touchSpeed": DEFAULT_TOUCH_SPEED,
        "smoothSlideAnime": False,
    }
    request = urllib.request.Request(
        HTTP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"MajdataView 返回 HTTP {response.status}")


def read_maidata_line(content: str, name: str, default: str = "") -> str:
    match = re.search(rf"&{re.escape(name)}=([^\n\r]*)", content)
    return match.group(1).strip() if match else default


def read_maidata_field(content: str, name: str, default: str = "") -> str:
    match = re.search(rf"&{re.escape(name)}=(.+?)(?:\r?\n&|$)", content, re.DOTALL)
    return match.group(1).strip() if match else default


def find_recording_image(work_dir: Path) -> Path | None:
    for stem in ("Cover", "bg"):
        for extension in (".png", ".jpg", ".jpeg"):
            path = work_dir / f"{stem}{extension}"
            if path.exists():
                return path
    return None


def majdatax_record_payload(song_dir: Path, difficulty: int, work_dir: Path,
                            pv_path: Path | None) -> tuple[dict, dict, dict]:
    content = (song_dir / "maidata.txt").read_text(encoding="utf-8")
    track = song_dir / "track.mp3"
    if not track.exists():
        raise FileNotFoundError(f"缺少音频: {track}")

    image_path = find_recording_image(work_dir)
    fumen = read_maidata_field(content, f"inote_{difficulty}")
    if not fumen:
        raise ValueError(f"maidata.txt 中缺少 &inote_{difficulty}")

    first = float(read_maidata_line(content, "first", "0") or 0)
    level = read_maidata_line(content, f"lv_{difficulty}", "0") or "0"
    designer = read_maidata_line(content, f"des_{difficulty}", "") or ""
    setting = {
        "ViewSetting": {
            "TapSpeed": DEFAULT_TAP_SPEED,
            "TouchSpeed": DEFAULT_TOUCH_SPEED,
            "SmoothSlideAnime": False,
            "BackgroundDim": DEFAULT_BACKGROUND_COVER,
            "ComboStatusType": 0,
            "JudgeDisplayMode": 0,
            "AutoMode": 0,
            "OutputFps": RECORD_FPS,
            "ResizeBg": False,
            "UIType": 0,
            "GlobalAudioOffset": 0,
            "LegacySlideLayer": False,
        },
        "VolumeSetting": {
            "Answer": 0.8,
            "Break": 0.7,
            "Slide": 0.3,
            "Tap": 0.45,
            "Touch": 0.7,
            "Track": 0.9,
        },
    }
    load = {
        "TrackPath": str(track),
        "ImagePath": str(image_path) if image_path else "",
        "VideoPath": str(pv_path) if pv_path else None,
    }
    play = {
        "Mode": 2,
        "StartAt": 0.0,
        "Speed": 1.0,
        "Title": read_maidata_line(content, "title", song_dir.name),
        "Artist": read_maidata_line(content, "artist", ""),
        "Offset": first,
        "Designer": designer,
        "Level": level,
        "Fumen": fumen,
        "Commands": [],
        "Difficulty": difficulty - 1,
        "MaidataPath": str(work_dir),
    }
    return setting, load, play


def post_record_majdatax(song_dir: Path, difficulty: int, work_dir: Path,
                         pv_path: Path | None) -> None:
    setting, load, play = majdatax_record_payload(song_dir, difficulty, work_dir, pv_path)
    with SimpleWebSocket(MAJDATA_X_WS_HOST, MAJDATA_X_WS_PORT, MAJDATA_X_WS_PATH, timeout=10) as ws:
        majdatax_request(ws, 0, setting, expected={200}, timeout=30)
        majdatax_request(ws, 1, load, expected={206}, timeout=60)
        majdatax_request(ws, 2, play, expected=None)


def video_is_complete(ffprobe: str, path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1024:
        return False
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return float(result.stdout.strip()) > 1
    except ValueError:
        return False


def video_has_picture(ffmpeg: str, path: Path) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-vf",
         "fps=1/15,signalstats,metadata=print:key=lavfi.signalstats.YMAX",
         "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    maxima = [int(value) for value in re.findall(r"YMAX=(\d+)", result.stderr)]
    return bool(maxima) and max(maxima) > 32


def crop_recorded_preview(ffmpeg: str, source: Path, output: Path) -> None:
    """Crop MajdataView's 16:9 recording to the center square gameplay area."""
    if not ENABLE_OUTPUT_CROP:
        if output.exists():
            output.unlink()
        source.replace(output)
        return

    temp_output = output.with_name(f"{output.stem}.cropping{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()
    if output.exists():
        output.unlink()

    # side = min(iw, ih); x/y choose the centered square.
    # For the current 2K recording this is crop=1440:1440:560:0.
    crop_filter = "crop='min(iw,ih)':'min(iw,ih)':'(iw-min(iw,ih))/2':'(ih-min(iw,ih))/2'"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-vf", crop_filter,
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(temp_output),
        ],
        check=True,
    )
    temp_output.replace(output)


def move_windows_offscreen(pid: int):
    """Keep Unity renderable without leaving its window over the user's work."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def move_if_owned(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.SetWindowPos(hwnd, 0, -2000, 0, 0, 0, 0x0001 | 0x0010 | 0x0040)
        return True

    user32.EnumWindows(callback_type(move_if_owned), 0)


def close_explorer_window_for_path(path: Path):
    """MajdataView opens Explorer on success; close only that folder window."""
    if os.name != "nt":
        return
    target = str(path.resolve())
    escaped_target = target.replace("'", "''")
    command = (
        "$shell = New-Object -ComObject Shell.Application; "
        "foreach ($window in @($shell.Windows())) { "
        "  try { "
        "    $folder = $window.Document.Folder.Self.Path; "
        f"    if ($folder -and $folder -eq '{escaped_target}') {{ $window.Quit() }} "
        "  } catch { } "
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def render_window_size() -> tuple[int, int]:
    # Unity's command-line size is already expressed in render pixels. Applying
    # Windows DPI scaling here silently reduced 4K/2K requests on HiDPI screens.
    return RECORD_WIDTH, RECORD_HEIGHT


def stop_process_tree(process: subprocess.Popen):
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def record_preview(majdata_home: Path, song_dir: Path, difficulty: int,
                   force: bool = False, timeout: int = 900) -> Path:
    output = preview_video_path(song_dir, difficulty)
    legacy_output = legacy_difficulty_path(song_dir, difficulty, "_preview.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not force:
        print(f"  [{song_dir.name}] {difficulty_name(difficulty)} 已存在，跳过")
        return output
    if legacy_output.exists() and not force:
        legacy_output.replace(output)
        print(f"  [{song_dir.name}] 已整理旧版 {legacy_output.name} -> {output.relative_to(song_dir)}")
        return output

    viewer = majdata_home / "MajdataView.exe"
    ffprobe = find_executable("ffprobe.exe", majdata_home) or find_executable("ffprobe", majdata_home)
    ffmpeg = find_executable("ffmpeg.exe", majdata_home) or find_executable("ffmpeg", majdata_home)
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe，无法确认视频编码是否完成")
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg，无法检查录制画面")

    configure_recorder(majdata_home)
    with tempfile.TemporaryDirectory(prefix=".majdata-record-", dir=song_dir) as temp_dir:
        work_dir = Path(temp_dir)
        raw_output = work_dir / "out.mp4"
        pv_path = prepare_recording_assets(ffmpeg, song_dir, work_dir)
        if pv_path:
            print(f"  [{song_dir.name}] 已生成恒定 {RECORD_FPS} FPS 的录制用 PV")

        window_width, window_height = render_window_size()
        process = subprocess.Popen(
            [str(viewer), "-screen-width", str(window_width),
             "-screen-height", str(window_height),
             "-screen-refresh-rate", str(RECORD_FPS), "-popupwindow"],
            cwd=majdata_home,
        )
        try:
            deadline = time.monotonic() + 90
            protocol: str | None = None
            while time.monotonic() < deadline:
                move_windows_offscreen(process.pid)
                if process.poll() is not None:
                    raise RuntimeError(f"MajdataView 提前退出，退出码 {process.returncode}")
                if http_ready():
                    protocol = "http"
                    break
                if majdatax_ws_ready():
                    protocol = "websocket"
                    break
                time.sleep(1)
            if protocol is None:
                raise TimeoutError("MajdataView 控制服务在 90 秒内未就绪")

            print(
                f"  [{song_dir.name}] 正在以 {window_width}x{window_height} "
                f"{RECORD_FPS} FPS 录制 {difficulty_name(difficulty)}..."
            )
            if protocol == "http":
                json_path = convert_maidata(
                    majdata_home, song_dir, difficulty, output_path=work_dir / "majdata.json"
                )
                prepare_audio(majdata_home, song_dir, work_dir, chart_json=json_path)
                post_record(json_path)
            else:
                post_record_majdatax(song_dir, difficulty, work_dir, pv_path)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if video_is_complete(ffprobe, raw_output):
                    if not video_has_picture(ffmpeg, raw_output):
                        raise RuntimeError("录制结果为纯黑画面")
                    close_explorer_window_for_path(work_dir)
                    crop_recorded_preview(ffmpeg, raw_output, output)
                    print(f"  [{song_dir.name}] 已生成 {output.relative_to(song_dir)}")
                    return output
                if process.poll() is not None:
                    raise RuntimeError(f"MajdataView 录制中退出，退出码 {process.returncode}")
                time.sleep(2)
            raise TimeoutError(f"录制超过 {timeout} 秒")
        finally:
            stop_process_tree(process)


def available_chart_difficulties(song_dir: Path) -> list[int]:
    maidata = song_dir / "maidata.txt"
    if not maidata.exists():
        return []
    return default_target_difficulties(parse_maidata(str(maidata)).charts)


def main():
    parser = argparse.ArgumentParser(description="使用 MajdataView 从 maidata.txt 生成谱面预览视频")
    parser.add_argument("-i", "--input", default=None, help="歌曲根目录")
    parser.add_argument("-d", "--dir", default=None, help="只处理指定曲目目录")
    parser.add_argument("-diff", "--difficulty", type=int, default=None,
                        help="难度 ID；不指定则默认只处理 MASTER/Re:MASTER")
    parser.add_argument("-f", "--force", action="store_true", help="覆盖已有预览视频")
    parser.add_argument("--install-only", action="store_true", help="只安装 MajdataView")
    parser.add_argument("--timeout", type=int, default=900, help="单曲录制超时秒数")
    args = parser.parse_args()

    if args.difficulty is not None and not 1 <= args.difficulty <= 7:
        parser.error("difficulty 必须在 1 到 7 之间")
    majdata_home = install_majdata_view()
    if args.install_only:
        print(f"MajdataView: {majdata_home}")
        return 0

    base = Path(args.input).resolve() if args.input else ROOT
    songs = [song.path for song in discover_song_folders(base, args.dir)]
    if not songs:
        print("未找到 maidata.txt", file=sys.stderr)
        return 1

    difficulty_label = (difficulty_name(args.difficulty)
                        if args.difficulty is not None else "默认 MASTER/Re:MASTER")
    print(f"发现 {len(songs)} 首歌曲, {difficulty_label}")

    failures = 0
    for song_dir in songs:
        difficulties = ([args.difficulty] if args.difficulty is not None
                        else available_chart_difficulties(song_dir))
        if not difficulties:
            print(f"  [{song_dir.name}] 未发现可录制的谱面难度", file=sys.stderr)
            failures += 1
            continue
        for difficulty in difficulties:
            try:
                record_preview(majdata_home, song_dir, difficulty, args.force, args.timeout)
            except Exception as exc:
                failures += 1
                print(
                    f"  [{song_dir.name}] {difficulty_name(difficulty)} 失败: {exc}",
                    file=sys.stderr,
                )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

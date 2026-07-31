"""Local FastAPI application for managing and viewing rhythm analyses."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (ConfigError, load_config, resolve_songs_root, save_config,
                     validate_config)
from .difficulty import (DIFFICULTY_NAMES, analysis_html_path,
                         difficulty_output_dir, meter_file_path,
                         preview_video_path, rhythm_svg_path,
                         sweep_maidata_path)
from .ffmpeg_capabilities import detect_capabilities, find_binary
from .meter import MeterMap
from .simai_parser import parse_maidata, parse_maidata_content
from .song_library import PROJECT_ROOT
from .sweep_marks import strip_sweep_markers
from .web_jobs import JobManager


WEB_ROOT = Path(__file__).resolve().parent / "web"
INSTANCE_FILE = PROJECT_ROOT / ".mra-web-instance.json"
_T = TypeVar("_T")


class JobRequest(BaseModel):
    song_id: str
    difficulty: int = Field(ge=1, le=7)
    force: bool = False


class TextPayload(BaseModel):
    content: str


class JsonPayload(BaseModel):
    data: dict[str, Any]


def _cleanup_temp_file(path: Path | None) -> None:
    """尽力清理临时文件；清理失败绝不能掩盖操作的真正结果。

    Windows 上杀毒软件可能短暂锁定刚写入的临时文件，某些沙箱环境会拦截
    删除操作。此时请求本身已经成功，不应因为删除失败而返回错误。
    """
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _is_retryable_file_error(exc: OSError) -> bool:
    """Windows 杀毒/索引器短暂占用文件时允许重试，其余错误立即上抛。"""
    return (
        getattr(exc, "winerror", None) in {5, 32, 33}
        or exc.errno in {errno.EACCES, errno.EBUSY, errno.EPERM}
    )


def _retry_file_operation(operation: Callable[[], _T]) -> _T:
    delays = (0.05, 0.1, 0.2, 0.4, 0.8)
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except OSError as exc:
            if attempt == len(delays) or not _is_retryable_file_error(exc):
                raise
            time.sleep(delays[attempt])


def _atomic_text(path: Path, content: str, *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.is_file():
        backup_path = path.with_suffix(path.suffix + ".bak")
        _retry_file_operation(lambda: shutil.copy2(path, backup_path))
    handle, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        _retry_file_operation(lambda: os.replace(temporary, path))
    finally:
        _cleanup_temp_file(temporary)


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _valid_sweep_marker_count(content: str) -> int:
    return len(re.findall(r"/S(?=[,/\s]|$)", content))


def _cover_path(song_dir: Path) -> Path | None:
    preferred = ("bg.png", "bg.jpg", "bg.jpeg", "cover.png", "cover.jpg", "jacket.png")
    files = {path.name.casefold(): path for path in song_dir.iterdir() if path.is_file()}
    for name in preferred:
        if name in files:
            return files[name]
    return None


def _safe_song(root: Path, song_id: str) -> Path:
    decoded = urllib.parse.unquote(song_id)
    if not decoded or Path(decoded).name != decoded or decoded in {".", ".."}:
        raise HTTPException(status_code=400, detail="无效歌曲编号")
    candidate = (root / decoded).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="歌曲路径越界") from exc
    if candidate.parent != root or not (candidate / "maidata.txt").is_file():
        raise HTTPException(status_code=404, detail="歌曲不存在")
    return candidate


def _safe_library_file(root: Path, relative: str) -> Path:
    candidate = (root / urllib.parse.unquote(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="资源路径越界") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return candidate


def _song_info(song_dir: Path) -> dict[str, Any]:
    song = parse_maidata(str(song_dir / "maidata.txt"))
    cover = _cover_path(song_dir)
    difficulties = []
    for difficulty, chart in sorted(song.charts.items()):
        output = difficulty_output_dir(song_dir, difficulty)
        difficulties.append({
            "id": difficulty,
            "name": DIFFICULTY_NAMES.get(difficulty, str(difficulty)),
            "level": chart.level,
            "designer": chart.designer,
            "outputs": {
                "meter": meter_file_path(song_dir, difficulty).is_file(),
                "preview": preview_video_path(song_dir, difficulty).is_file(),
                "rhythm": rhythm_svg_path(song_dir, difficulty).is_file(),
                "analysis": analysis_html_path(song_dir, difficulty).is_file(),
                "directory": output.is_dir(),
            },
        })
    return {
        "id": song_dir.name,
        "title": song.title or song_dir.name,
        "artist": song.artist,
        "bpm": song.bpm,
        "genre": song.genre,
        "version": song.version,
        "cover_url": (
            f"/library/{urllib.parse.quote(song_dir.name)}/{urllib.parse.quote(cover.name)}"
            if cover else None
        ),
        "sweep_exists": sweep_maidata_path(song_dir).is_file(),
        "difficulties": difficulties,
    }


def _maidata_field(content: str, name: str, default: str = "") -> str:
    match = re.search(
        rf"^&{re.escape(name)}=(.*)$",
        content,
        flags=re.MULTILINE,
    )
    return match.group(1).strip() if match else default


def _song_summary(song_dir: Path) -> dict[str, Any]:
    """Read only maidata headers; the library page must not parse every note."""
    content = (song_dir / "maidata.txt").read_text(encoding="utf-8-sig")
    cover = _cover_path(song_dir)
    levels = {
        int(match.group(1)): match.group(2).strip()
        for match in re.finditer(r"^&lv_([1-7])=(.*)$", content, re.MULTILINE)
    }
    designers = {
        int(match.group(1)): match.group(2).strip()
        for match in re.finditer(r"^&des_([1-7])=(.*)$", content, re.MULTILINE)
    }
    charts = {
        int(match.group(1))
        for match in re.finditer(r"^&inote_([1-7])=", content, re.MULTILINE)
    }
    difficulties = []
    for difficulty in sorted(charts):
        difficulties.append({
            "id": difficulty,
            "name": DIFFICULTY_NAMES.get(difficulty, str(difficulty)),
            "level": levels.get(difficulty, ""),
            "designer": designers.get(difficulty, ""),
            "outputs": {
                "meter": meter_file_path(song_dir, difficulty).is_file(),
                "preview": preview_video_path(song_dir, difficulty).is_file(),
                "rhythm": rhythm_svg_path(song_dir, difficulty).is_file(),
                "analysis": analysis_html_path(song_dir, difficulty).is_file(),
                "directory": difficulty_output_dir(song_dir, difficulty).is_dir(),
            },
        })
    bpm_text = _maidata_field(content, "wholebpm", "0")
    try:
        bpm: float | str = float(bpm_text)
    except ValueError:
        bpm = bpm_text
    return {
        "id": song_dir.name,
        "title": _maidata_field(content, "title", song_dir.name),
        "artist": _maidata_field(content, "artist"),
        "bpm": bpm,
        "genre": _maidata_field(content, "genre"),
        "version": _maidata_field(content, "version"),
        "cover_url": (
            f"/library/{urllib.parse.quote(song_dir.name)}/{urllib.parse.quote(cover.name)}"
            if cover else None
        ),
        "sweep_exists": sweep_maidata_path(song_dir).is_file(),
        "difficulties": difficulties,
    }


def create_app(
    *,
    config_file: str | Path | None = None,
    songs_root: str | Path | None = None,
) -> FastAPI:
    config, warnings = load_config(config_file)
    root = Path(songs_root).resolve() if songs_root else resolve_songs_root(config)
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Maimai Rhythm Analysis", version="1.0")
    app.state.config_file = Path(config_file).resolve() if config_file else None
    app.state.config = config
    app.state.config_warnings = warnings
    app.state.songs_root = root
    app.state.jobs = JobManager(PROJECT_ROOT)
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/system")
    def system(refresh: bool = Query(default=False)):
        # render_preview imports NumPy/SciPy. Keep that cost out of the web
        # server's startup path and pay it only when the environment card is
        # requested.
        from .render_preview import default_majdata_home

        ffmpeg = find_binary("ffmpeg")
        ffprobe = find_binary("ffprobe")
        capabilities = None
        capability_error = None
        if ffmpeg:
            try:
                capabilities = detect_capabilities(
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    preference=app.state.config["encoder"],
                    refresh=refresh,
                ).to_dict()
            except Exception as exc:
                capability_error = str(exc)
        majdata = default_majdata_home()
        return {
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "capabilities": capabilities,
            "capability_error": capability_error,
            "majdata_home": str(majdata),
            "majdata_available": (majdata / "MajdataView.exe").is_file(),
            "config_warnings": app.state.config_warnings,
        }

    @app.get("/api/songs")
    def songs():
        result = []
        for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not directory.is_dir() or not (directory / "maidata.txt").is_file():
                continue
            try:
                result.append(_song_summary(directory))
            except Exception as exc:
                result.append({
                    "id": directory.name,
                    "title": directory.name,
                    "error": str(exc),
                    "difficulties": [],
                })
        return {"songs": result}

    @app.get("/api/songs/{song_id}")
    def song(song_id: str):
        return _song_info(_safe_song(root, song_id))

    @app.get("/api/songs/{song_id}/meter/{difficulty}")
    def get_meter(song_id: str, difficulty: int):
        song_dir = _safe_song(root, song_id)
        song = parse_maidata(str(song_dir / "maidata.txt"))
        if difficulty not in song.charts:
            raise HTTPException(status_code=404, detail="歌曲没有这个难度")
        path = meter_file_path(song_dir, difficulty)
        if not path.is_file():
            return {
                "exists": False,
                "data": {
                    "version": 2,
                    "default": "4/4",
                    "difficulty": difficulty,
                    "sections": [{
                        "start_beat": 0,
                        "signature": "4/4",
                        "confidence": 1.0,
                        "source": "manual",
                    }],
                },
            }
        try:
            return {"exists": True, "data": json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/songs/{song_id}/meter/{difficulty}")
    def put_meter(song_id: str, difficulty: int, payload: JsonPayload):
        song_dir = _safe_song(root, song_id)
        song = parse_maidata(str(song_dir / "maidata.txt"))
        if difficulty not in song.charts:
            raise HTTPException(status_code=404, detail="歌曲没有这个难度")
        try:
            sections = payload.data.get("sections") or payload.data.get("measures") or []
            starts = [float(section["start_beat"]) for section in sections]
            if any(start < 0 for start in starts):
                raise ValueError("start_beat 不能小于 0")
            if any(right <= left for left, right in zip(starts, starts[1:])):
                raise ValueError("拍号节点必须按 start_beat 严格递增")
            meter = MeterMap.from_dict(payload.data)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"拍号配置无效: {exc}") from exc
        data = meter.to_dict(difficulty)
        path = meter_file_path(song_dir, difficulty)
        _atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return {"saved": True, "data": data}

    @app.get("/api/songs/{song_id}/sweep")
    def get_sweep(song_id: str):
        song_dir = _safe_song(root, song_id)
        path = sweep_maidata_path(song_dir)
        source = path if path.is_file() else song_dir / "maidata.txt"
        content = source.read_text(encoding="utf-8")
        return {
            "exists": path.is_file(),
            "content": content,
            "markers": _valid_sweep_marker_count(content),
            "sha256": _sha256_text(content),
        }

    @app.put("/api/songs/{song_id}/sweep")
    def put_sweep(song_id: str, payload: TextPayload):
        song_dir = _safe_song(root, song_id)
        path = sweep_maidata_path(song_dir)
        baseline_path = path if path.is_file() else song_dir / "maidata.txt"
        baseline = baseline_path.read_text(encoding="utf-8")
        if strip_sweep_markers(payload.content) != strip_sweep_markers(baseline):
            raise HTTPException(
                status_code=422,
                detail="只能增删 /S 标记，不能在此处修改谱面时间结构",
            )
        try:
            parsed = parse_maidata_content(payload.content)
            if not parsed.charts:
                raise ValueError("文件中没有可用谱面")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"扫键标记文件无效: {exc}") from exc
        try:
            _atomic_text(path, payload.content)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"扫键标记文件暂时无法写入，请稍后重试: {exc}",
            ) from exc
        return {
            "saved": True,
            "markers": _valid_sweep_marker_count(payload.content),
            "sha256": _sha256_text(payload.content),
        }

    @app.get("/api/settings")
    def get_settings():
        return app.state.config

    @app.put("/api/settings")
    def put_settings(payload: JsonPayload):
        try:
            validated = validate_config(payload.data)
            saved = save_config(validated, app.state.config_file)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app.state.config = saved
        return {"saved": True, "restart_required": True, "data": saved}

    @app.post("/api/jobs")
    def create_job(payload: JobRequest):
        song_dir = _safe_song(root, payload.song_id)
        song = parse_maidata(str(song_dir / "maidata.txt"))
        if payload.difficulty not in song.charts:
            raise HTTPException(status_code=422, detail="歌曲没有这个难度")
        return app.state.jobs.submit(
            song_dir, payload.difficulty, payload.force,
        ).to_dict()

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": [job.to_dict() for job in app.state.jobs.list()]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        result = app.state.jobs.get(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return result.to_dict()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str):
        if app.state.jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        def stream():
            previous = ""
            while True:
                current = app.state.jobs.get(job_id)
                if current is None:
                    return
                payload = json.dumps(current.to_dict(), ensure_ascii=False)
                if payload != previous:
                    yield f"data: {payload}\n\n"
                    previous = payload
                if current.status in {"completed", "failed", "cancelled"}:
                    return
                time.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        result = app.state.jobs.cancel(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return result.to_dict()

    @app.get("/library/{relative:path}")
    def library(relative: str):
        return FileResponse(_safe_library_file(root, relative))

    @app.exception_handler(ConfigError)
    async def config_error_handler(_request, exc: ConfigError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


def _free_port(host: str) -> int:
    with socket.socket() as server:
        server.bind((host, 0))
        return int(server.getsockname()[1])


def _running_instance_url() -> str | None:
    try:
        state = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        url = str(state["url"])
        with urllib.request.urlopen(f"{url}api/health", timeout=0.6) as response:
            if response.status == 200:
                return url
    except Exception:
        return None
    return None


def _cleanup_stale_videos(songs_root: Path, max_age_days: int = 5, dry_run: bool = False):
    """Delete preview and strip videos older than *max_age_days*.

    扫描所有歌曲目录中的 preview.mp4 和 strip_video.mp4，
    删除 mtime 超过 max_age_days 天的文件。
    """
    now = time.time()
    cutoff = now - max_age_days * 86400
    deleted_bytes = 0
    deleted_count = 0
    patterns = ["outputs/*/video/preview.mp4", "outputs/*/strip/strip_video*.mp4"]

    if not songs_root.is_dir():
        return

    for song_dir in sorted(songs_root.iterdir()):
        if not song_dir.is_dir():
            continue
        for pattern in patterns:
            for candidate in song_dir.glob(pattern):
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    size = candidate.stat().st_size if not dry_run else 0
                    if not dry_run:
                        try:
                            candidate.unlink()
                            if candidate.name.startswith("strip_video"):
                                metadata_path = candidate.with_name(
                                    "strip_video.json"
                                )
                                try:
                                    metadata = json.loads(
                                        metadata_path.read_text(encoding="utf-8")
                                    )
                                except (OSError, ValueError, TypeError):
                                    metadata = {}
                                if metadata.get("filename") == candidate.name:
                                    metadata_path.unlink(missing_ok=True)
                        except OSError as exc:
                            print(f"  无法删除 {candidate}: {exc}")
                            continue
                    deleted_bytes += size
                    deleted_count += 1

    if deleted_count > 0:
        mb = deleted_bytes / (1024 * 1024)
        print(f"视频清理: 已删除 {deleted_count} 个超过 {max_age_days} 天的视频文件"
              f" ({mb:.1f} MB)" + (" [试运行]" if dry_run else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Maimai Rhythm Analysis 本地 Web 应用")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    config, warnings = load_config()
    for warning in warnings:
        print(f"警告: {warning}")

    # 启动时清理超过 5 天的视频文件
    songs_root = resolve_songs_root(config)
    _cleanup_stale_videos(songs_root)

    existing = _running_instance_url()
    if existing:
        print(f"Web 应用已经在运行: {existing}")
        if not args.no_browser:
            webbrowser.open(existing)
        return 0
    host = args.host or config["web"]["host"]
    configured_port = config["web"]["port"] if args.port is None else args.port
    port = configured_port or _free_port(host)
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        if exc.name != "uvicorn":
            raise
        print(
            "缺少 Web 服务依赖 Uvicorn。请先在当前 Python 环境运行：\n"
            f'  "{sys.executable}" -m pip install -r "{PROJECT_ROOT / "requirements.txt"}"',
            file=sys.stderr,
        )
        return 2
    should_open = config["web"]["open_browser"] and not args.no_browser
    if should_open:
        threading.Thread(
            target=lambda: (time.sleep(0.8), webbrowser.open(f"http://{host}:{port}/")),
            daemon=True,
        ).start()
    print(f"Maimai Rhythm Analysis: http://{host}:{port}/", flush=True)
    url = f"http://{host}:{port}/"
    _atomic_text(
        INSTANCE_FILE,
        json.dumps({"pid": os.getpid(), "url": url}, ensure_ascii=False),
        backup=False,
    )
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="info")
    finally:
        try:
            state = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
            if state.get("pid") == os.getpid():
                INSTANCE_FILE.unlink()
        except (OSError, ValueError, TypeError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

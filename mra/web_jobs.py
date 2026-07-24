"""Single-worker background queue for long-running local analysis jobs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AnalysisJob:
    id: str
    song_dir: Path
    difficulty: int
    force: bool
    status: str = "queued"
    progress: float = 0.0
    step: str = ""
    logs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    manifest: str | None = None
    error: str | None = None
    process: subprocess.Popen | None = field(default=None, repr=False)
    future: Future | None = field(default=None, repr=False)
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "song": self.song_dir.name,
            "difficulty": self.difficulty,
            "force": self.force,
            "status": self.status,
            "progress": self.progress,
            "step": self.step,
            "logs": list(self.logs[-500:]),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "manifest": self.manifest,
            "error": self.error,
        }


class JobManager:
    """Run one MajdataView workflow at a time and retain recent job state."""

    def __init__(self, project_root: Path, max_jobs: int = 100) -> None:
        self.project_root = project_root.resolve()
        self.max_jobs = max_jobs
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mra-job")

    def submit(self, song_dir: Path, difficulty: int, force: bool = False) -> AnalysisJob:
        job = AnalysisJob(
            id=uuid.uuid4().hex,
            song_dir=song_dir.resolve(),
            difficulty=int(difficulty),
            force=bool(force),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
            job.future = self._executor.submit(self._run, job)
        return job

    def list(self) -> list[AnalysisJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.cancel_requested = True
            if job.status == "queued" and job.future and job.future.cancel():
                job.status = "cancelled"
                job.finished_at = _now()
                return job
            process = job.process
        if process and process.poll() is None:
            self._stop_process_tree(process)
        return job

    def _trim(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        finished = sorted(
            (job for job in self._jobs.values()
             if job.status in {"completed", "failed", "cancelled"}),
            key=lambda job: job.created_at,
        )
        for job in finished[:max(0, len(self._jobs) - self.max_jobs)]:
            self._jobs.pop(job.id, None)

    def _append_log(self, job: AnalysisJob, line: str) -> None:
        clean = line.rstrip()
        if not clean:
            return
        with self._lock:
            job.logs.append(clean)
            if len(job.logs) > 1000:
                del job.logs[:250]

    def _apply_event(self, job: AnalysisJob, event: dict) -> None:
        kind = event.get("event")
        with self._lock:
            if kind == "progress":
                job.progress = max(0.0, min(1.0, float(event.get("value", 0))))
                job.step = str(event.get("step", job.step))
            elif kind == "step":
                job.step = str(event.get("name", job.step))
            elif kind == "completed":
                job.manifest = event.get("manifest")
            elif kind == "error":
                job.error = str(event.get("message", "分析失败"))

    def _run(self, job: AnalysisJob) -> None:
        with self._lock:
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = _now()
                return
            job.status = "running"
            job.started_at = _now()
        command = [
            sys.executable, "-m", "mra.desktop_backend", "analyze",
            "--song-dir", str(job.song_dir),
            "--difficulty", str(job.difficulty),
            "--json-progress",
        ]
        if job.force:
            command.append("--force")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._lock:
                job.process = process
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(job, line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._apply_event(job, event)
            return_code = process.wait()
            with self._lock:
                if job.cancel_requested:
                    job.status = "cancelled"
                elif return_code == 0:
                    job.status = "completed"
                    job.progress = 1.0
                else:
                    job.status = "failed"
                    job.error = job.error or f"处理进程退出码 {return_code}"
        except Exception as exc:
            with self._lock:
                job.status = "cancelled" if job.cancel_requested else "failed"
                job.error = str(exc)
                self._append_log(job, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                job.process = None
                job.finished_at = _now()

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

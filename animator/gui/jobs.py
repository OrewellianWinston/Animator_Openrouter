"""Runs one CLI generation at a time as a subprocess and tracks its progress."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS_DIR = REPO_ROOT / "outputs"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
MAX_LOG_LINES = 5000

ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]*$")


class JobBusy(Exception):
    """A job is already running."""


@dataclass
class JobSpec:
    prompt: str
    scenes: Optional[int]
    quality: str
    preset: str
    text_model: str
    region_model: str
    image_model: str
    tts_model: str
    voice: str


CommandBuilder = Callable[[JobSpec, Path], list]


def slugify(text: str, limit: int = 40) -> str:
    """Lowercase a-z0-9 words joined by dashes, at most `limit` characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "untitled"


def cli_python() -> str:
    """The console interpreter (pythonw has no usable std streams for the CLI)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def build_command(spec: JobSpec, video: Path) -> list:
    """Argv for the real CLI. `--opt=value` form keeps odd values from being parsed as flags."""
    cmd = [
        cli_python(), "-m", "animator",
        f"--prompt={spec.prompt}", "-o", str(video),
        f"--quality={spec.quality}",
        f"--text-model={spec.text_model}", f"--region-model={spec.region_model}",
        f"--image-model={spec.image_model}", f"--tts-model={spec.tts_model}",
        f"--voice={spec.voice}", "-v", "--save-storyboard",
    ]
    if spec.scenes:
        cmd.append(f"--scenes={spec.scenes}")
    return cmd


def humanize_error(text: str) -> str:
    """Turn a raw CLI/OpenRouter error line into something readable."""
    low = text.lower()
    if "needs openrouter_api_key" in low:
        return "No API key set"
    if "insufficient" in low or "402" in low or "credits" in low:
        return "OpenRouter: insufficient credits"
    if "401" in low or "invalid api key" in low or "no auth" in low or "unauthorized" in low:
        return "OpenRouter: the API key was rejected"
    if "429" in low or "rate limit" in low:
        return "OpenRouter: rate limited, try again in a moment"
    if "ffmpeg" in low and "not found" in low:
        return "FFmpeg is not installed or not on PATH"
    return text.strip()[:400]


@dataclass
class Job:
    """State of one run; mutated by the reader thread, read under `lock`."""
    id: str
    folder: Path
    spec: JobSpec
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    status: str = "running"          # running | done | failed | cancelled
    step: str = "storyboard"         # storyboard | scenes | render | done
    total: Optional[int] = None
    ready: int = 0
    duration: Optional[float] = None
    error: Optional[str] = None
    log: list = field(default_factory=list)
    version: int = 0
    proc: Optional[subprocess.Popen] = None
    cancelled: bool = False
    last_error_line: Optional[str] = None

    def public(self) -> dict:
        """JSON view for the UI."""
        return {
            "id": self.id, "status": self.status, "step": self.step,
            "total": self.total, "ready": self.ready,
            "prompt": self.spec.prompt, "preset": self.spec.preset,
            "started": self.started, "finished": self.finished,
            "duration": self.duration, "error": self.error,
        }

    def feed(self, line: str) -> None:
        """Parse one output line into progress."""
        if m := re.search(r"storyboard: (\d+) scene", line):
            self.total, self.step = int(m.group(1)), "scenes"
        elif re.search(r"scene \d+ ready:", line):
            self.ready += 1
            if self.total and self.ready >= self.total:
                self.step = "render"
        elif m := re.search(r"^wrote .+ \(([\d.]+)s, \d+ scene", line):
            self.duration, self.step = float(m.group(1)), "done"
        elif "animator: error:" in line or re.match(r"^[\w.]*(Error|Exception|Exit)\b.*", line):
            self.last_error_line = line.strip()


class JobManager:
    """Single-slot job runner."""

    def __init__(self, outputs: Path = OUTPUTS_DIR, command_builder: CommandBuilder = build_command,
                 env_provider: Callable[[], dict] = dict) -> None:
        self.outputs = outputs
        self.command_builder = command_builder
        self.env_provider = env_provider
        self.current: Optional[Job] = None
        self.lock = threading.Lock()

    def start(self, spec: JobSpec) -> Job:
        """Create the output folder and launch the subprocess. Raises JobBusy."""
        with self.lock:
            if self.current and self.current.status == "running":
                raise JobBusy()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            job_id = f"{stamp}-{slugify(spec.prompt)}"
            folder = self.outputs / job_id
            folder.mkdir(parents=True, exist_ok=False)
            job = Job(id=job_id, folder=folder, spec=spec)
            self._write_json(job)
            self.current = job
        env = {**os.environ, **self.env_provider(), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            job.proc = subprocess.Popen(
                self.command_builder(spec, folder / "video.mp4"),
                cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self._finish(job, error=str(exc))
            return job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def cancel(self) -> bool:
        """Kill the running job's process tree. Returns False when nothing runs."""
        job = self.current
        if not job or job.status != "running" or not job.proc:
            return False
        job.cancelled = True
        kill_tree(job.proc)
        return True

    def snapshot(self, job: Job, start: int) -> tuple:
        """(version, public state, log lines from `start`) under the lock."""
        with self.lock:
            return job.version, job.public(), job.log[start:]

    def _pump(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        for raw in job.proc.stdout:
            line = raw.rstrip("\r\n")
            with self.lock:
                job.log.append(line)
                if len(job.log) > MAX_LOG_LINES:
                    del job.log[: MAX_LOG_LINES // 5]
                job.feed(line)
                job.version += 1
        code = job.proc.wait()
        tail = [x for x in job.log if x.strip()][-3:]
        error = None
        if code != 0:
            error = humanize_error(job.last_error_line or " | ".join(tail) or f"exited with code {code}")
        self._finish(job, error=error)

    def _finish(self, job: Job, error: Optional[str]) -> None:
        with self.lock:
            job.finished = time.time()
            if job.cancelled:
                job.status, job.error = "cancelled", None
            elif error or job.step != "done":
                job.status, job.error = "failed", error or "Finished without producing a video"
            else:
                job.status = "done"
            job.version += 1
            self._write_json(job)

    @staticmethod
    def _write_json(job: Job) -> None:
        s = job.spec
        data = {
            "id": job.id, "prompt": s.prompt, "preset": s.preset,
            "models": {"text": s.text_model, "region": s.region_model,
                       "image": s.image_model, "tts": s.tts_model},
            "voice": s.voice, "scenes": s.scenes, "scenes_total": job.total, "quality": s.quality,
            "started": datetime.fromtimestamp(job.started).isoformat(timespec="seconds"),
            "finished": datetime.fromtimestamp(job.finished).isoformat(timespec="seconds") if job.finished else None,
            "status": job.status, "duration": job.duration, "error": job.error,
        }
        (job.folder / "job.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the process and its children."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    else:
        proc.kill()

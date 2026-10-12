"""FastAPI app behind the desktop window: starts jobs, streams progress, serves history."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .jobs import ID_PATTERN, OUTPUTS_DIR, REPO_ROOT, JobBusy, JobManager, JobSpec

STATIC_DIR = Path(__file__).parent / "static"
ENV_PATH = REPO_ROOT / ".env"
KEY_NAME = "OPENROUTER_API_KEY"
API = "https://openrouter.ai/api/v1"

# Keep "middle" in sync with animator/ai/settings.py defaults.
PRESETS: dict = {
    "budget": {
        "label": "Budget", "per_scene": 0.012, "voice": "af_heart",
        "models": {"text": "anthropic/claude-haiku-5.5", "region": "qwen/qwen3-vl-235b-a22b-instruct",
                   "image": "recraft/recraft-v4.1-flash", "tts": "hexgrad/kokoro-82m"},
    },
    "middle": {
        "label": "Middle", "per_scene": 0.055, "voice": "sarah",
        "models": {"text": "anthropic/claude-sonnet-5.5", "region": "qwen/qwen3-vl-235b-a22b-instruct",
                   "image": "recraft/recraft-v4.1", "tts": "elevenlabs/eleven-v4-turbo"},
    },
    "premium": {
        "label": "Premium", "per_scene": 0.20, "voice": "sarah",
        "models": {"text": "anthropic/claude-sonnet-5.5", "region": "qwen/qwen3-vl-235b-a22b-instruct",
                   "image": "openai/gpt-image-2", "tts": "elevenlabs/eleven-v4"},
    },
    "custom": {"label": "Custom", "per_scene": None, "voice": "sarah", "models": {}},
}
DEFAULT_PRESET = "middle"

SLUG = r"^[A-Za-z0-9][A-Za-z0-9._:/+~-]{0,99}$"


class Models(BaseModel):
    text: str = Field(pattern=SLUG)
    region: str = Field(pattern=SLUG)
    image: str = Field(pattern=SLUG)
    tts: str = Field(pattern=SLUG)


class JobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    scenes: Optional[int] = Field(default=None, ge=1, le=8)
    quality: Literal["low", "medium", "high"] = "medium"
    preset: Literal["budget", "middle", "premium", "custom"] = DEFAULT_PRESET
    models: Models
    voice: str = Field(pattern=SLUG)


class KeyRequest(BaseModel):
    key: str


# ---------- API key / .env ----------

def read_api_key(env_path: Path = ENV_PATH) -> str:
    """The key from .env, else the process environment ('' when unset)."""
    file_key = (dotenv_values(env_path).get(KEY_NAME) or "").strip() if env_path.is_file() else ""
    return file_key or os.environ.get(KEY_NAME, "").strip()


def mask_key(key: str) -> Optional[str]:
    return f"…{key[-4:]}" if key else None


def valid_key(key: str) -> bool:
    return key.startswith("sk-or-") and len(key) >= 16 and re.fullmatch(r"[\x21-\x7e]+", key) is not None


def write_env_key(env_path: Path, key: str) -> None:
    """Set OPENROUTER_API_KEY in `env_path`, keeping every other line."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    new = f"{KEY_NAME}={key}"
    pattern = re.compile(rf"^\s*(export\s+)?{KEY_NAME}\s*=")
    replaced = False
    out = []
    for line in lines:
        if pattern.match(line):
            if not replaced:
                out.append(new)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new)
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)


# ---------- OpenRouter lookups ----------

class TTLCache:
    """Tiny single-value cache."""

    def __init__(self, ttl: float) -> None:
        self.ttl, self.value, self.at = ttl, None, 0.0

    def get(self):
        return self.value if self.value is not None and time.time() - self.at < self.ttl else None

    def set(self, value) -> None:
        self.value, self.at = value, time.time()


def fetch_balance(key: str) -> dict:
    """Dollars left, from /credits (fallback /key)."""
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=10) as client:
        r = client.get(f"{API}/credits", headers=headers)
        if r.status_code == 200:
            data = r.json().get("data", {})
            total, used = float(data.get("total_credits", 0)), float(data.get("total_usage", 0))
            return {"remaining": round(total - used, 4), "total": total, "used": used}
        r = client.get(f"{API}/key", headers=headers)
        r.raise_for_status()
        data = r.json().get("data", {})
        left = data.get("limit_remaining")
        return {"remaining": None if left is None else round(float(left), 4),
                "total": data.get("limit"), "used": data.get("usage")}


def normalize_voices(raw) -> list:
    voices = []
    for item in raw or []:
        name = item if isinstance(item, str) else (item.get("id") or item.get("name") or "")
        if name:
            voices.append(str(name))
    return voices


# ---------- history ----------

def history_dir(outputs: Path, item_id: str) -> Path:
    """Resolve a history id to its folder or raise 404."""
    if not ID_PATTERN.match(item_id):
        raise HTTPException(404, "not found")
    folder = (outputs / item_id).resolve()
    if folder.parent != outputs.resolve() or not folder.is_dir():
        raise HTTPException(404, "not found")
    return folder


def scene_images(folder: Path) -> list:
    assets = folder / "video.storyboard"
    return sorted(p.name for p in assets.glob("scene_*.png")) if assets.is_dir() else []


def read_history_item(folder: Path, running_id: Optional[str]) -> Optional[dict]:
    try:
        data = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("status") == "running" and data.get("id") != running_id:
        data["status"], data["error"] = "failed", "Interrupted before it finished"
    data["id"] = folder.name
    data["has_video"] = (folder / "video.mp4").is_file()
    data["images"] = len(scene_images(folder))
    return data


# ---------- app ----------

def create_app(port: int, manager: Optional[JobManager] = None, outputs: Path = OUTPUTS_DIR) -> FastAPI:
    """Build the app. Only Host/Origin values for 127.0.0.1:port and localhost:port are accepted."""
    manager = manager or JobManager(outputs, env_provider=lambda: {KEY_NAME: read_api_key()})
    app = FastAPI(title="Animator", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    balance_cache, voices_cache = TTLCache(30), {"all": None}
    voices_lock = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """DNS-rebinding and cross-site guard."""
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "bad host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "bad origin"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state() -> dict:
        key = read_api_key()
        job = manager.current
        return {
            "job": job.public() if job else None,
            "key": {"set": bool(key), "tail": mask_key(key)},
            "presets": PRESETS, "default_preset": DEFAULT_PRESET,
            "default_voice": PRESETS[DEFAULT_PRESET]["voice"],
        }

    # --- jobs ---

    @app.post("/api/jobs")
    def start_job(req: JobRequest) -> dict:
        if not read_api_key():
            raise HTTPException(400, "No API key set")
        m = req.models
        spec = JobSpec(req.prompt.strip(), req.scenes, req.quality, req.preset,
                       m.text, m.region, m.image, m.tts, req.voice)
        try:
            return manager.start(spec).public()
        except JobBusy:
            raise HTTPException(409, "A video is already being generated")

    @app.get("/api/jobs/current")
    def current_job() -> dict:
        if not manager.current:
            raise HTTPException(404, "no job")
        return manager.current.public()

    @app.get("/api/jobs/current/events")
    async def job_events(request: Request) -> StreamingResponse:
        job = manager.current
        if not job:
            raise HTTPException(404, "no job")

        async def stream():
            sent, seen = 0, -1
            while True:
                version, public, lines = manager.snapshot(job, sent)
                if version != seen:
                    seen = version
                    if lines:
                        yield sse("log", {"start": sent, "lines": lines})
                        sent += len(lines)
                    yield sse("state", public)
                if public["status"] != "running" and not lines:
                    yield sse("end", public)
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    @app.post("/api/jobs/current/cancel")
    def cancel_job() -> dict:
        if not manager.cancel():
            raise HTTPException(409, "nothing is running")
        return {"ok": True}

    # --- history ---

    @app.get("/api/history")
    def history() -> list:
        running = manager.current.id if manager.current and manager.current.status == "running" else None
        folders = [p for p in outputs.glob("*") if p.is_dir() and ID_PATTERN.match(p.name)] if outputs.is_dir() else []
        items = [read_history_item(f, running) for f in sorted(folders, key=lambda p: p.name, reverse=True)]
        return [i for i in items if i]

    @app.get("/api/history/{item_id}/video")
    def history_video(item_id: str) -> FileResponse:
        video = history_dir(outputs, item_id) / "video.mp4"
        if not video.is_file():
            raise HTTPException(404, "no video")
        return FileResponse(video, media_type="video/mp4")

    @app.get("/api/history/{item_id}/image/{n}")
    def history_image(item_id: str, n: int) -> FileResponse:
        images = scene_images(history_dir(outputs, item_id))
        if not 0 <= n < len(images):
            raise HTTPException(404, "no image")
        return FileResponse(outputs / item_id / "video.storyboard" / images[n], media_type="image/png")

    @app.delete("/api/history/{item_id}")
    def delete_history(item_id: str) -> dict:
        folder = history_dir(outputs, item_id)
        if manager.current and manager.current.status == "running" and manager.current.id == item_id:
            raise HTTPException(409, "that video is still being generated")
        shutil.rmtree(folder)
        return {"ok": True}

    @app.post("/api/history/{item_id}/open")
    def open_history(item_id: str) -> dict:
        folder = history_dir(outputs, item_id)
        if sys.platform != "win32":
            raise HTTPException(501, "opening folders is only supported on Windows")
        os.startfile(folder)  # noqa: S606 - folder path is validated above
        return {"ok": True}

    # --- OpenRouter lookups and settings ---

    @app.get("/api/balance")
    def balance() -> dict:
        key = read_api_key()
        if not key:
            raise HTTPException(400, "No API key set")
        cached = balance_cache.get()
        if cached:
            return cached
        try:
            result = fetch_balance(key)
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "Could not read the balance from OpenRouter")
        balance_cache.set(result)
        return result

    @app.get("/api/voices")
    def voices(model: str) -> dict:
        with voices_lock:
            if voices_cache["all"] is None:
                try:
                    r = httpx.get(f"{API}/models", params={"output_modalities": "speech"}, timeout=15)
                    r.raise_for_status()
                    voices_cache["all"] = r.json().get("data", [])
                except (httpx.HTTPError, ValueError):
                    return {"voices": []}
        for entry in voices_cache["all"]:
            if entry.get("id") == model:
                return {"voices": normalize_voices(entry.get("supported_voices"))}
        return {"voices": []}

    @app.post("/api/settings/key")
    def save_key(req: KeyRequest) -> dict:
        key = req.key.strip()
        if not valid_key(key):
            raise HTTPException(400, "That does not look like an OpenRouter key (it starts with sk-or-)")
        write_env_key(ENV_PATH, key)
        balance_cache.value = None
        return {"set": True, "tail": mask_key(key)}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

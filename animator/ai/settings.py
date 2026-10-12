"""Model defaults and retry settings for every AI call (all OpenRouter slugs).

Each model can be overridden with an environment variable; CLI flags win over both.
"""
import os

DEFAULT_TEXT_MODEL = os.getenv("ANIMATOR_TEXT_MODEL") or "anthropic/claude-haiku-5.5"
DEFAULT_REGION_MODEL = os.getenv("ANIMATOR_REGION_MODEL") or "qwen/qwen3-vl-235b-a22b-instruct"
DEFAULT_IMAGE_MODEL = os.getenv("ANIMATOR_IMAGE_MODEL") or "recraft/recraft-v4.1-flash"
DEFAULT_TTS_MODEL = os.getenv("ANIMATOR_TTS_MODEL") or "hexgrad/kokoro-82m"
DEFAULT_VOICE = os.getenv("ANIMATOR_VOICE") or "af_heart"

MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "5"))
RETRY_DELAY_SEC = float(os.getenv("AI_RETRY_DELAY_SEC", "8"))

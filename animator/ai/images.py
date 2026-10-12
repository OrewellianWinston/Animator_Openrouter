"""STEP 2: draw one storyboard scene as a 16:9 whiteboard image."""
from __future__ import annotations

import base64
import io

import numpy as np
import openai
from PIL import Image

from ..prompts import load_prompt
from .client import AIClient, with_retry
from .schemas import StoryboardScene
from .settings import DEFAULT_IMAGE_MODEL

TARGET_RATIO = 16 / 9
RATIO_TOLERANCE = 0.02


def build_image_prompt(scene: StoryboardScene) -> str:
    elements = "\n".join(
        f'{index}. {beat.visual.strip()} — caption "{beat.label.strip().upper()}"'
        for index, beat in enumerate(scene.beats, start=1)
    )
    frame = load_prompt("scene_image").format(
        title=scene.title.strip().upper(),
        count=len(scene.beats),
        elements=elements,
        layout=scene.layout.strip(),
    )
    return f"{frame}\n{load_prompt('image_style')}"


def _field(obj, name):
    """Read `name` from a dict or an SDK object (extra fields live in model_extra)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


def extract_image_bytes(response) -> bytes:
    """Decode the first generated image (a base64 data URL) in a chat completion."""
    choices = getattr(response, "choices", None) or []
    message = choices[0].message if choices else None
    for item in _field(message, "images") or []:
        url = _field(_field(item, "image_url"), "url")
        if url:
            return base64.b64decode(url.split(",", 1)[1] if url.startswith("data:") else url)
    text = _field(message, "content") or ""
    raise RuntimeError(f"Image generation returned no image. raw: {str(text)[:500]}")


def pad_to_16x9(image_bytes: bytes) -> bytes:
    """PNG bytes at exactly 16:9. Off-ratio images are padded (never cropped, so
    captions survive) with the median colour of their border, centred."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    width, height = image.size
    if abs(width / height / TARGET_RATIO - 1) > RATIO_TOLERANCE:
        pixels = np.asarray(image)
        edge = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
        fill = tuple(int(v) for v in np.median(edge, axis=0))
        if width / height > TARGET_RATIO:
            canvas_w, canvas_h = width, round(width * 9 / 16)
        else:
            canvas_w, canvas_h = round(height * 16 / 9), height
        canvas = Image.new("RGB", (canvas_w, canvas_h), fill)
        canvas.paste(image, ((canvas_w - width) // 2, (canvas_h - height) // 2))
        image = canvas
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def generate_scene_image(
    client: AIClient,
    scene: StoryboardScene,
    *,
    model: str = DEFAULT_IMAGE_MODEL,
) -> bytes:
    """Render one frame of separate flat doodle icons. Returns 16:9 PNG bytes."""
    prompt = build_image_prompt(scene)

    def request(modalities):
        return client.sync.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"modalities": modalities, "image_config": {"aspect_ratio": "16:9"}},
        )

    def call():
        try:
            return request(["image"])
        except openai.BadRequestError:
            # models that emit text alongside the picture reject image-only output
            return request(["image", "text"])

    return pad_to_16x9(extract_image_bytes(with_retry(call)))

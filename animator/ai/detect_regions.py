"""STEP 4: detect a region plan (what to draw, where, and when) from a
finished whiteboard image."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, List

from PIL import Image

from ..prompts import load_prompt
from ..regions import Region, SnippetRegionPlan
from .client import JSON_SYSTEM_MESSAGE, AIClient, json_response_format, request_structured_async
from .schemas import DetectedRegion, DetectionResponse
from .settings import DEFAULT_REGION_MODEL


def build_region_prompt(transcript_text: str, idea: str, beats: List[str] | None = None) -> str:
    prompt = load_prompt("region_detection").format(idea=idea, transcript_text=transcript_text)
    if not beats:
        return prompt
    numbered = "\n".join(f"  {index}. {text}" for index, text in enumerate(beats, start=1))
    return f"{prompt}\n\n{load_prompt('region_beats').format(numbered=numbered)}"


def to_region(detected: DetectedRegion) -> Region:
    return Region(
        label=detected.label,
        role=detected.role,
        object=detected.object,
        reveal_order=detected.reveal_order,
        box=detected.box,
        expected_visual=detected.object,
        annotation=detected.annotation,
        reveal=detected.reveal,
        beat=detected.beat or None,
    )


def sanitize_boxes(data: Any) -> Any:
    """Clamp every box value to 0-1000 and swap min/max that came back reversed,
    so one sloppy coordinate does not reject the whole plan."""
    regions = data.get("regions") if isinstance(data, dict) else None
    for region in regions if isinstance(regions, list) else []:
        box = region.get("box") if isinstance(region, dict) else None
        if not isinstance(box, dict):
            continue
        for key in ("ymin", "xmin", "ymax", "xmax"):
            if isinstance(box.get(key), (int, float)):
                box[key] = int(round(min(1000, max(0, box[key]))))
        for low, high in (("ymin", "ymax"), ("xmin", "xmax")):
            if low in box and high in box and box[low] > box[high]:
                box[low], box[high] = box[high], box[low]
    if not regions:
        # vision models occasionally answer with an empty list; treat it as bad output so it is retried
        raise ValueError("no regions in response")
    return data


async def detect_region_plan(
    *,
    client: AIClient,
    image_path: Path,
    transcript_text: str,
    idea: str = "",
    model: str = DEFAULT_REGION_MODEL,
    beats: List[str] | None = None,
) -> SnippetRegionPlan:
    idea = idea or "(untitled)"
    with Image.open(image_path) as image:
        size = list(image.size)
        mime_type = Image.MIME.get(image.format, "image/png")
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode()}"
    prompt = build_region_prompt(transcript_text, idea, beats)

    parsed = await request_structured_async(
        lambda: client.aio.chat.completions.create(
            model=model,
            messages=[JSON_SYSTEM_MESSAGE, {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]}],
            temperature=0,
            response_format=json_response_format("region_detection", DetectionResponse),
        ),
        DetectionResponse,
        "Region detection",
        prepare=sanitize_boxes,
    )
    if not parsed.regions:
        raise RuntimeError("Region detection returned no regions.")

    return SnippetRegionPlan(
        idea=idea,
        global_style_notes="",
        regions=[to_region(detected) for detected in parsed.regions],
        image_size=size,
    )

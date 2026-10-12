"""STEP 1: turn one idea into a storyboard of scenes made of narrated beats."""
from __future__ import annotations

import logging
from typing import List, Optional

from ..prompts import load_prompt
from .client import AIClient, json_response_format, request_structured
from .schemas import StoryboardPlan
from .settings import DEFAULT_TEXT_MODEL

logger = logging.getLogger(__name__)


def build_storyboard_prompt(idea: str, num_scenes: Optional[int]) -> str:
    scene_count = (
        f"Produce exactly {num_scenes} scenes."
        if num_scenes
        else "Produce as many scenes as needed to explain it well, usually 3-6."
    )
    return load_prompt("storyboard").format(idea=idea, scene_count=scene_count)


def build_retry_prompt(idea: str, num_scenes: Optional[int], unspoken: List[str]) -> str:
    retry_note = load_prompt("storyboard_retry").format(unspoken=unspoken)
    return f"{build_storyboard_prompt(idea, num_scenes)}\n\n{retry_note}"


def unspoken_labels(plan: StoryboardPlan) -> List[str]:
    return [beat.label for scene in plan.scenes for beat in scene.beats if not beat.label_is_spoken]


def request_storyboard(client: AIClient, prompt: str, model: str) -> StoryboardPlan:
    plan = request_structured(
        lambda: client.sync.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=json_response_format("storyboard", StoryboardPlan),
        ),
        StoryboardPlan,
        "Storyboard generation",
    )
    if not plan.scenes or not all(scene.beats for scene in plan.scenes):
        raise RuntimeError(f"Storyboard generation returned no scenes. raw: {plan.model_dump_json()[:500]}")
    return plan


def generate_storyboard(
    client: AIClient,
    idea: str,
    *,
    model: str = DEFAULT_TEXT_MODEL,
    num_scenes: Optional[int] = None,
) -> StoryboardPlan:
    """Write the script. Retries once when a caption's words are never spoken,
    because the viewer should hear each caption as it appears; keeps whichever
    draft has fewer such captions."""
    prompt = build_storyboard_prompt(idea, num_scenes)
    best: Optional[StoryboardPlan] = None
    for _ in range(2):
        draft = request_storyboard(client, prompt, model)
        if best is None or len(unspoken_labels(draft)) < len(unspoken_labels(best)):
            best = draft
        unspoken = unspoken_labels(best)
        if not unspoken:
            break
        logger.warning("captions never spoken in their sentence: %s", unspoken)
        prompt = build_retry_prompt(idea, num_scenes, unspoken)
    return best

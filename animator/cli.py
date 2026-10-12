"""Command line entry point: python -m animator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .regions import SnippetRegionPlan
from .render import Scene, render_video


def load_plan(path: str | None) -> SnippetRegionPlan | None:
    if not path:
        return None
    try:
        return SnippetRegionPlan.model_validate_json(Path(path).read_text())
    except (ValidationError, UnicodeError) as exc:
        raise ValueError(f"invalid region plan '{path}': {exc}") from exc


def validate_output(output: Path, other_inputs: list[str]) -> None:
    if output.suffix.lower() != ".mp4":
        raise ValueError("--output must end in .mp4")
    if not output.parent.is_dir():
        raise ValueError(f"output directory does not exist: {output.parent}; create it first")
    if output.is_dir():
        raise ValueError(f"output path is a directory: {output}")
    if output.resolve() in {Path(p).resolve() for p in other_inputs}:
        raise ValueError("--output must be different from every input file")


def require_tools(*, need_ffprobe: bool) -> None:
    required = ["ffmpeg"] + (["ffprobe"] if need_ffprobe else [])
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise ValueError(
            f"{', '.join(missing)} not found on PATH. Install FFmpeg "
            "(macOS: brew install ffmpeg; Ubuntu/Debian: sudo apt install ffmpeg; "
            "Windows: https://ffmpeg.org/download.html), then reopen your terminal."
        )


def validate_inputs(args: argparse.Namespace) -> None:
    if not args.audio and (not math.isfinite(args.duration) or args.duration <= 0):
        raise ValueError("--duration must be a finite number greater than zero")
    for path in [*args.images, *args.audio, *args.regions]:
        if not Path(path).is_file():
            raise ValueError(f"input file not found: {path}")
    for path in args.images:
        try:
            with Image.open(path) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"cannot read image '{path}'; use a valid PNG or JPEG image") from exc
    validate_output(Path(args.output), [*args.images, *args.audio, *args.regions])
    require_tools(need_ffprobe=bool(args.audio))


def ai_client() -> "AIClient":
    try:
        from .ai.client import make_client
    except ImportError as exc:
        raise SystemExit(
            "this needs the openai package: pip install -r requirements.txt"
        ) from exc
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("needs OPENROUTER_API_KEY in the environment (or in .env)")
    return make_client(api_key)


def detect_plan(image: Path, narration: str, model: str | None) -> SnippetRegionPlan:
    """`model` is the --region-model flag; the env var and built-in default sit behind it."""
    from .ai.detect_regions import detect_region_plan
    from .ai.settings import DEFAULT_REGION_MODEL

    return asyncio.run(detect_region_plan(
        client=ai_client(),
        image_path=image,
        transcript_text=narration,
        model=model or DEFAULT_REGION_MODEL,
    ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m animator",
        description="Animate whiteboard-style images as hand-drawn reveal videos.",
    )
    parser.add_argument("images", nargs="*", help="scene images in order (omit when using --prompt)")
    parser.add_argument("-o", "--output", required=True, help="output MP4")
    parser.add_argument("--audio", nargs="*", default=[],
                        help="one narration file per image, in the same order")
    parser.add_argument("--duration", type=float, default=8.0,
                        help="seconds per scene when there is no audio (default 8)")
    parser.add_argument("--quality", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--regions", nargs="*", default=[],
                        help="one region plan JSON per image, in the same order")
    parser.add_argument("--detect-regions", action="store_true",
                        help="ask an AI vision model for a region plan per image (needs OPENROUTER_API_KEY)")
    parser.add_argument("--narration", nargs="*", default=[],
                        help="narration text per image, used by --detect-regions")
    parser.add_argument("--text-model", default=None,
                        help="OpenRouter model for the --prompt storyboard "
                             "(default: $ANIMATOR_TEXT_MODEL or a built-in default)")
    parser.add_argument("--region-model", default=None,
                        help="OpenRouter vision model that finds regions in images "
                             "(default: $ANIMATOR_REGION_MODEL or a built-in default)")
    parser.add_argument("--save-regions", action="store_true",
                        help="write detected plans next to the output as <output>.regions.<n>.json")
    parser.add_argument("--prompt", default=None,
                        help="generate a full storyboard (images, narration, regions) from one idea "
                             "via OpenRouter, instead of passing images; needs OPENROUTER_API_KEY")
    parser.add_argument("--scenes", type=int, default=None,
                        help="number of scenes when using --prompt (default: let the model decide)")
    parser.add_argument("--voice", default=None,
                        help="TTS voice for --prompt narration (default: $ANIMATOR_VOICE or 'sarah'). "
                             "Voices are model-specific, e.g. Kokoro 'af_heart', 'am_adam'; ElevenLabs 'sarah'; see "
                             "https://openrouter.ai/api/v1/models?output_modalities=speech "
                             "(supported_voices)")
    parser.add_argument("--image-model", default=None,
                        help="OpenRouter image model for --prompt scene art "
                             "(default: $ANIMATOR_IMAGE_MODEL or a built-in default)")
    parser.add_argument("--tts-model", default=None,
                        help="OpenRouter speech model for --prompt narration "
                             "(default: $ANIMATOR_TTS_MODEL or a built-in default)")
    parser.add_argument("--save-storyboard", action="store_true",
                        help="keep generated images/audio/regions next to the output "
                             "instead of discarding them after rendering")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_from_prompt(args: argparse.Namespace) -> int:
    from .ai.pipeline import StoryboardOptions, run_storyboard
    from .ai.settings import (
        DEFAULT_IMAGE_MODEL, DEFAULT_REGION_MODEL, DEFAULT_TEXT_MODEL, DEFAULT_TTS_MODEL, DEFAULT_VOICE,
    )

    output = Path(args.output)
    validate_output(output, [])
    require_tools(need_ffprobe=True)

    options = StoryboardOptions(
        idea=args.prompt,
        output=output,
        quality=args.quality,
        text_model=args.text_model or DEFAULT_TEXT_MODEL,
        region_model=args.region_model or DEFAULT_REGION_MODEL,
        image_model=args.image_model or DEFAULT_IMAGE_MODEL,
        tts_model=args.tts_model or DEFAULT_TTS_MODEL,
        voice=args.voice or DEFAULT_VOICE,
        num_scenes=args.scenes,
        save_assets=args.save_storyboard,
    )
    durations = run_storyboard(ai_client(), options)
    print(f"wrote {args.output} ({sum(durations):.1f}s, {len(durations)} scene(s))", file=sys.stderr)
    return 0


def run_cli(args: argparse.Namespace) -> int:
    if args.prompt:
        if args.images:
            raise SystemExit("pass images, or --prompt, not both")
        if args.audio or args.regions or args.detect_regions:
            raise SystemExit(
                "--audio/--regions/--detect-regions aren't used with --prompt; "
                "the storyboard generates its own images, narration, and regions"
            )
        return run_from_prompt(args)

    if not args.images:
        raise SystemExit("provide image(s), or use --prompt to generate a storyboard")

    n = len(args.images)
    if args.audio and len(args.audio) != n:
        raise SystemExit(f"expected {n} audio files, got {len(args.audio)}")
    if args.regions and len(args.regions) != n:
        raise SystemExit(f"expected {n} region plans, got {len(args.regions)}")
    if args.regions and args.detect_regions:
        raise SystemExit("use --regions or --detect-regions, not both")
    if args.narration and len(args.narration) != n:
        raise SystemExit(f"expected {n} narration strings, got {len(args.narration)}")

    validate_inputs(args)
    scenes = []
    for index, image in enumerate(args.images):
        plan = load_plan(args.regions[index]) if args.regions else None
        if args.detect_regions:
            narration = args.narration[index] if args.narration else ""
            plan = detect_plan(Path(image), narration, args.region_model)
            if args.save_regions:
                plan_path = f"{args.output}.regions.{index}.json"
                Path(plan_path).write_text(plan.model_dump_json(indent=2))
                print(f"wrote {plan_path}", file=sys.stderr)
        scenes.append(Scene(
            image=image,
            audio=args.audio[index] if args.audio else None,
            duration=None if args.audio else args.duration,
            region_plan=plan,
        ))

    durations = render_video(scenes, args.output, quality=args.quality)
    print(f"wrote {args.output} ({sum(durations):.1f}s, {len(durations)} scene(s))", file=sys.stderr)
    return 0


def load_env_file() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return run_cli(args)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or ""
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        message = f"{Path(exc.cmd[0]).name} failed: {detail.strip() or exc}"
    except (OSError, ValueError, RuntimeError) as exc:
        message = str(exc)
    print(f"animator: error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

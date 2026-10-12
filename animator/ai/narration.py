"""STEP 3: speak a scene's beats as one take and time each beat in it."""
from __future__ import annotations

import shutil
import subprocess
from typing import List, Tuple

from .beat_timing import PCM_RATE, locate_beats, pcm_to_wav
from .client import AIClient, with_retry
from .settings import DEFAULT_TTS_MODEL, DEFAULT_VOICE


def decode_to_pcm(audio: bytes) -> bytes:
    """Any audio format -> mono 16-bit PCM at PCM_RATE, which beat timing assumes."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH; it is needed to decode the narration audio")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(PCM_RATE), "pipe:1"],
        input=audio, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode(errors="replace")[:500]
        raise RuntimeError(f"ffmpeg could not decode the narration audio: {detail}")
    return proc.stdout


def synthesize_pcm(client: AIClient, text: str, model: str, voice: str) -> bytes:
    """Raw speech: mono 16-bit PCM at 24kHz."""
    response = with_retry(lambda: client.sync.audio.speech.create(
        model=model, voice=voice, input=text, response_format="mp3",
    ))
    audio = response.content if hasattr(response, "content") else response.read()
    if not audio:
        raise RuntimeError("Narration synthesis returned no audio")
    return decode_to_pcm(audio)


def generate_beat_narration(
    client: AIClient,
    beats: List[str],
    *,
    model: str = DEFAULT_TTS_MODEL,
    voice: str = DEFAULT_VOICE,
) -> Tuple[bytes, List[List[float]]]:
    """Synthesize a scene's beats as one take and locate each beat in it.

    Returns (wav_bytes, windows) where windows[i] is the [start, end] in
    seconds of beat i inside the WAV, so drawing can be locked to speech.
    One request per scene keeps the voice natural and stays inside TTS rate
    limits; beat boundaries are recovered from the pauses between sentences.
    """
    pcm = synthesize_pcm(client, " ".join(text.strip() for text in beats), model, voice)
    return pcm_to_wav(pcm), locate_beats(pcm, beats)

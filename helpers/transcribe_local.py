"""Transcribe a video locally with faster-whisper.

Drop-in alternative to transcribe.py that emits the same JSON shape Scribe
produces, so pack_transcripts.py and render.py keep working unchanged.

Limitations vs. ElevenLabs Scribe:
  - No speaker diarization (every word gets speaker_id "speaker_0").
  - No audio-event tags (no "(laughs)", "(sighs)", etc.).
  - Whisper tends to drop disfluencies; the initial_prompt nudges it toward
    verbatim but does not match Scribe's verbatim mode.

Usage:
    python helpers/transcribe_local.py <video_path>
    python helpers/transcribe_local.py <video_path> --model medium
    python helpers/transcribe_local.py <video_path> --language en
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


VERBATIM_PROMPT = (
    "Um, uh, hmm, you know, like, I mean, so, well, actually, basically, "
    "right? Okay, yeah."
)


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def whisper_to_scribe_words(segments) -> list[dict]:
    """Convert faster-whisper segments → flat Scribe-shape word list.

    Inserts a 'spacing' entry between any two consecutive words separated by
    a real gap. pack_transcripts.py uses those spacing entries to detect
    silences ≥ 0.5s for phrase grouping; render.py's SRT builder ignores them.
    """
    words: list[dict] = []
    prev_end: float | None = None
    for seg in segments:
        if not getattr(seg, "words", None):
            continue
        for w in seg.words:
            start = float(w.start) if w.start is not None else None
            end = float(w.end) if w.end is not None else None
            text = (w.word or "").strip()
            if start is None or end is None or not text:
                continue
            if prev_end is not None and start > prev_end:
                words.append({
                    "type": "spacing",
                    "text": " ",
                    "start": prev_end,
                    "end": start,
                })
            words.append({
                "type": "word",
                "text": text,
                "start": start,
                "end": end,
                "speaker_id": "speaker_0",
            })
            prev_end = end
    return words


def transcribe_one(
    video: Path,
    edit_dir: Path,
    model_name: str = "large-v3",
    language: str | None = None,
    verbose: bool = True,
    model=None,
    **_kwargs,
) -> Path:
    """Transcribe a single video locally. Returns path to transcript JSON.

    Cached: returns existing path if the transcript already exists.
    Pass a preloaded WhisperModel via `model=` to skip per-call load cost.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if model is None:
        from faster_whisper import WhisperModel
        if verbose:
            print(f"  loading model: {model_name} (int8, cpu)", flush=True)
        model = WhisperModel(model_name, device="cpu", compute_type="int8")

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio)
        if verbose:
            print(f"  transcribing {audio.name}", flush=True)
        segments, info = model.transcribe(
            str(audio),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=VERBATIM_PROMPT,
        )
        segments = list(segments)

    words = whisper_to_scribe_words(segments)
    payload = {
        "language_code": info.language,
        "language_probability": info.language_probability,
        "words": words,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        spoken = sum(1 for w in words if w["type"] == "word")
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        print(f"    words: {spoken}  (language: {info.language})")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video locally with faster-whisper")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <video_parent>/edit)")
    ap.add_argument("--model", type=str, default="large-v3",
                    help="faster-whisper model (large-v3, medium, small, base, tiny). Default large-v3.")
    ap.add_argument("--language", type=str, default=None,
                    help="Optional ISO language code (e.g. 'en'). Omit to auto-detect.")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        model_name=args.model,
        language=args.language,
    )


if __name__ == "__main__":
    main()

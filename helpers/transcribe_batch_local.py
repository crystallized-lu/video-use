"""Batch-transcribe every video in a directory with local faster-whisper.

Loads the WhisperModel once and reuses it across all files — model load is
expensive (~3GB RAM for large-v3) and parallel workers on a single machine
don't help.

Cached per-file: any source that already has a transcript is skipped.

Usage:
    python helpers/transcribe_batch_local.py <videos_dir>
    python helpers/transcribe_batch_local.py <videos_dir> --model medium
    python helpers/transcribe_batch_local.py <videos_dir> --edit-dir /custom/edit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from transcribe_local import transcribe_one


VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV", ".mkv", ".MKV", ".avi", ".AVI", ".m4v"}


def find_videos(videos_dir: Path) -> list[Path]:
    return sorted(
        p for p in videos_dir.iterdir()
        if p.is_file() and p.suffix in VIDEO_EXTS
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch local transcription with faster-whisper")
    ap.add_argument("videos_dir", type=Path, help="Directory containing source videos")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="Edit output directory (default: <videos_dir>/edit)")
    ap.add_argument("--model", type=str, default="large-v3",
                    help="faster-whisper model (default: large-v3)")
    ap.add_argument("--language", type=str, default=None,
                    help="Optional ISO language code. Omit to auto-detect per file.")
    args = ap.parse_args()

    videos_dir = args.videos_dir.resolve()
    if not videos_dir.is_dir():
        sys.exit(f"not a directory: {videos_dir}")

    edit_dir = (args.edit_dir or (videos_dir / "edit")).resolve()
    (edit_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    videos = find_videos(videos_dir)
    if not videos:
        sys.exit(f"no videos found in {videos_dir}")

    cached = [v for v in videos if (edit_dir / "transcripts" / f"{v.stem}.json").exists()]
    pending = [v for v in videos if v not in cached]

    print(f"found {len(videos)} videos ({len(cached)} cached, {len(pending)} to transcribe)")
    if not pending:
        print("nothing to do")
        return

    from faster_whisper import WhisperModel
    print(f"loading model: {args.model} (int8, cpu) — this is a one-time cost")
    t_load = time.time()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    print(f"  model loaded in {time.time() - t_load:.1f}s")

    t0 = time.time()
    errors: list[tuple[Path, str]] = []
    for i, video in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}] {video.name}")
        try:
            transcribe_one(
                video=video,
                edit_dir=edit_dir,
                model_name=args.model,
                language=args.language,
                model=model,
                verbose=True,
            )
        except Exception as e:
            errors.append((video, str(e)))
            print(f"  x FAILED: {e}")

    print(f"\ndone in {time.time() - t0:.1f}s")
    if errors:
        print(f"{len(errors)} failures:")
        for v, msg in errors:
            print(f"  {v.name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()

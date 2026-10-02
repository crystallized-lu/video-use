"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --skin-fix      # iPhone HLG→SDR red/detail fix
    python helpers/render.py <edl.json> -o final.mp4 --hdr-preserve  # keep HDR (phone look / Instagram)
    python helpers/render.py <edl.json> -o final.mp4 --low-compute   # gentle on the machine
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# CPU cap injected into every heavy ffmpeg encode. Empty = ffmpeg auto (all
# cores). Set by --threads / --low-compute so a render can stay out of the way
# of foreground work on the machine.
FF_THREAD_FLAGS: list[str] = []

# iPhone HLG→SDR skin correction (opt-in via --skin-fix). The HDR tonemap
# oversaturates skin (reads as a red/flushed face) AND flattens micro-contrast
# (skin looks waxy, pores/lines gone); the default downscaler softens further.
# This neutralises the red (global saturation pull + a slight red→neutral white
# balance) and restores pore detail (lanczos downscale + light unsharp). Keep the
# tonemap at desat=0 — desat>0 makes pale specular blotches on oily skin. Verified
# frame-for-frame against source on real iPhone HLG selfie footage, 2026-08.
SKIN_FIX_CHAIN = (
    "colorbalance=rm=-0.06:rs=-0.04:bm=0.03,"
    "eq=saturation=0.74,"
    "unsharp=5:5:0.6:5:5:0.0"
)

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


# -------- HDR-preserve (keep the phone look) --------------------------------
#
# The opposite choice to tone-mapping. Instagram/iOS do not flatten HDR to SDR —
# they carry the video HDR end-to-end, which is why an uploaded clip looks like
# the phone. Tone-mapping HLG→SDR *cannot* match that (wider gamut + brightness
# squeezed into Rec.709), and no grade recovers it. So --hdr-preserve skips the
# tonemap entirely, keeps everything 10-bit, and encodes HEVC (VideoToolbox,
# main10) with the source's color tags intact. Captions burn on the HLG stream.
# Caveat: this keeps HLG *static* HDR; the source's Dolby Vision RPU (per-frame
# dynamic metadata) is dropped — full DoVi passthrough needs a different encoder.
# Verified against source on-device (iPhone HLG selfie, 2026-09). For SDR
# deliverables use --skin-fix instead.


def source_color_tags(video: Path) -> list[str]:
    """ffmpeg args stamping the output with the source's HDR color tags, so an
    HDR-preserve render keeps the HLG/PQ signaling players need. Defaults to HLG
    Rec.2020 when the probe comes back empty/unknown."""
    d: dict[str, str] = {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_primaries,color_transfer,color_space",
             "-of", "default=noprint_wrappers=1", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass

    def pick(key: str, default: str) -> str:
        v = d.get(key, "")
        return v if v and v != "unknown" else default

    return ["-color_primaries", pick("color_primaries", "bt2020"),
            "-color_trc",        pick("color_transfer", "arib-std-b67"),
            "-colorspace",       pick("color_space", "bt2020nc"),
            "-color_range",      "tv"]


def display_dimensions(video: Path) -> tuple[int, int]:
    """(width, height) as the frame DISPLAYS, after any rotation metadata.

    iPhone (and many phones) store landscape pixels plus a 90° display-matrix
    rotation, so a vertical clip's raw stream width/height still read as
    landscape. This ffmpeg build does not expose that rotation reliably through
    ffprobe (side_data comes back empty), so we decode the first frame — ffmpeg
    auto-applies the rotation — and read its true size from `showinfo`. Falls
    back to raw stream dims if the decode fails.
    """
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(video),
             "-vf", "showinfo", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        m = re.search(r"\bs:(\d+)x(\d+)", out.stderr)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(","))
        return w, h
    except Exception:
        return 0, 0


def is_portrait_source(video: Path) -> bool:
    """Return True if the video DISPLAYS as portrait (height > width),
    accounting for rotation metadata (see display_dimensions)."""
    w, h = display_dimensions(video)
    return h > w


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    fps: int = 24,
    lossless: bool = False,
    audio_bitrate: str = "192k",
    skin_fix: bool = False,
    hdr_preserve: bool = False,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # HDR-preserve wins over skin-fix (which is the SDR-tonemap correction).
    hdr = hdr_preserve and is_hdr_source(source)

    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"
    if skin_fix or hdr:
        scale += ":flags=lanczos"   # sharper downscale — recovers fine detail

    vf_parts: list[str] = []
    if is_hdr_source(source) and not hdr:
        vf_parts.append(TONEMAP_CHAIN)    # HLG/PQ → Rec.709 SDR (skipped when preserving HDR)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    if skin_fix and not hdr:
        vf_parts.append(SKIN_FIX_CHAIN)   # colour-neutralise + unsharp, applied last
    if hdr:
        vf_parts.append("format=yuv420p10le")   # stay 10-bit through the intermediate
    vf = ",".join(vf_parts)

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    # apad+atrim: audio exactly `duration` long, matching the frame-exact video,
    # so no per-segment shortfall accumulates across the concat.
    af = (f"apad,atrim=0:{duration:.6f},"
          f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03")

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        "ffmpeg", *FF_THREAD_FLAGS, "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-frames:v", str(round(duration * fps)),   # exact frame count: video == audio length
        "-vf", vf,
        "-af", af,
        # Lossless intermediate (hq): the segment encode then costs nothing and
        # the final composite is the only generation of loss. Measured on this
        # footage: lossless -> CRF 14 scores identically to a single encode.
        *(["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"] if lossless
          else ["-c:v", "libx264", "-preset", preset, "-crf", crf]),
        "-pix_fmt", "yuv420p10le" if hdr else "yuv420p", "-r", str(fps),
        *(source_color_tags(source) if hdr else []),
        # PCM, not AAC: every AAC segment carries encoder priming/padding, and the
        # -c copy concat stacks it into a growing audio lag (measured +0.36 s over
        # 23 segments). PCM concatenates sample-exact; audio is encoded once, later.
        "-c:a", "pcm_s16le", "-ar", "48000",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: int = 24,
    lossless: bool = False,
    audio_bitrate: str = "192k",
    skin_fix: bool = False,
    hdr_preserve: bool = False,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mov.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        # Whole frames, so the segment's video and audio are the same length;
        # otherwise each concat joint adds up to one frame of A/V offset.
        duration = round((end - start) * fps) / fps
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mov"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path,
                        preview=preview, draft=draft, fps=fps,
                        lossless=lossless, audio_bitrate=audio_bitrate,
                        skin_fix=skin_fix, hdr_preserve=hdr_preserve)
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
    audio_bitrate: str = "192k",
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True,
                                      audio_bitrate=audio_bitrate)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    crf: str = "18",
    preset: str = "fast",
    fps: int = 24,
    hdr_preserve: bool = False,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
             "-movflags", "+faststart", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Zero the video start timestamp. Concat leaves the video track starting a
    # fraction of a frame late; each re-encode compounds it, and the muxer
    # records the result as an edit-list delay on the video track only — so the
    # audio plays ahead of the picture. Harmless-looking at 20ms, clearly
    # audible as lip-sync drift once it reaches a whole frame.
    filter_parts.append("[0:v]setpts=PTS-STARTPTS[vbase]")

    # Chain overlays on top of base
    current = "[vbase]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        # .ass files carry their own [V4+ Styles]; force_style would override
        # custom per-event tags (per-word color, font swap). Apply force_style
        # only to .srt where the file has no embedded styling.
        if subtitles_path.suffix.lower() == ".ass":
            filter_parts.append(
                f"{current}subtitles='{subs_abs}'[outv]"
            )
        else:
            filter_parts.append(
                f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
            )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    if hdr_preserve:
        # Keep the HDR: HEVC main10 via VideoToolbox, 10-bit, source color tags
        # carried through from the base (the segments were extracted 10-bit with
        # the HLG/PQ tags). hvc1 tag so Apple players recognize it. No H.264 level
        # dance — HEVC HDR is played by the phone's HEVC decoder, not the H.264 one.
        vcodec = [
            "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
            "-pix_fmt", "p010le", "-tag:v", "hvc1",
            "-b:v", "20M" if preset == "slow" else "14M",
            *source_color_tags(base_path),
        ]
    else:
        vcodec = [
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            # Device compatibility. Without an explicit level x264 signalled 6.2 for
            # 1080x1920@30, which no iPhone hardware decoder accepts — the file
            # simply refuses to play and iMovie will not import it. 4.2 covers this
            # resolution and frame rate with room to spare. The bitrate cap keeps
            # the stream honest against the level it declares; at CRF 14 the average
            # is ~5 Mbps, so 20M never binds. CFR because iMovie dislikes variable
            # frame rate, which concatenated segments otherwise produce.
            "-profile:v", "high", "-level:v", "4.2",
            "-maxrate", "20M", "-bufsize", "40M",
        ]

    cmd = [
        "ffmpeg", *FF_THREAD_FLAGS, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        *vcodec,
        "-fps_mode", "cfr", "-r", str(fps),
        "-c:a", "aac", "-b:a", "320k",   # base audio is PCM (see extract_segment)
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--fps",
        type=int,
        default=24,
        help="Output frame rate (default 24). Use the source rate to avoid frame "
             "decimation, which reads as lip-sync drift on talking heads.",
    )
    ap.add_argument(
        "--hq",
        action="store_true",
        help="Highest quality: lossless segment intermediates, CRF 14 preset slow "
             "final encode, 320k audio. Larger temp files and a slower render.",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--skin-fix",
        action="store_true",
        help="iPhone HLG skin correction: neutralise the red the HDR tonemap adds to "
             "skin and restore pore detail (lanczos downscale + light unsharp). "
             "Recommended for iPhone HDR selfie footage; harmless on other sources.",
    )
    ap.add_argument(
        "--hdr-preserve",
        action="store_true",
        help="Keep HDR instead of tone-mapping to SDR: no tonemap, 10-bit HEVC "
             "(main10) output carrying the source's HLG/PQ tags. Matches the "
             "on-phone look for HDR displays / Instagram (which preserves HDR "
             "end-to-end). Drops Dolby Vision RPU (HLG static HDR kept). Overrides "
             "--skin-fix. For SDR deliverables use --skin-fix instead.",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=0,
        help="Cap ffmpeg/filter threads per encode (0 = auto/all cores). Lower it to "
             "keep the machine responsive during a render.",
    )
    ap.add_argument(
        "--low-compute",
        action="store_true",
        help="Run gently: 2 threads + lowest process priority (nice 19). Much slower "
             "wall time, but leaves the machine usable. Overrides --threads.",
    )
    args = ap.parse_args()

    # CPU throttling: cap ffmpeg threads and (for --low-compute) de-prioritise the
    # whole process tree so foreground work stays responsive.
    threads = 2 if args.low_compute else args.threads
    if threads:
        global FF_THREAD_FLAGS
        FF_THREAD_FLAGS = ["-threads", str(threads),
                           "-filter_threads", str(threads),
                           "-filter_complex_threads", str(threads)]
    if args.low_compute:
        try:
            os.nice(19)
        except OSError:
            pass

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    if args.hdr_preserve and args.skin_fix:
        print("note: --hdr-preserve overrides --skin-fix (skin-fix is the SDR path)")

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps,
        lossless=args.hq, audio_bitrate=("320k" if args.hq else "192k"),
        skin_fix=args.skin_fix, hdr_preserve=args.hdr_preserve,
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mov"
    elif args.preview:
        base_name = "base_preview.mov"
    else:
        base_name = "base.mov"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    comp_crf, comp_preset = ("14", "slow") if args.hq else ("18", "fast")
    audio_bitrate = "320k" if args.hq else "192k"
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir,
                              crf=comp_crf, preset=comp_preset, fps=args.fps,
                              hdr_preserve=args.hdr_preserve)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir,
                              crf=comp_crf, preset=comp_preset, fps=args.fps,
                              hdr_preserve=args.hdr_preserve)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft,
                                audio_bitrate=audio_bitrate)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()

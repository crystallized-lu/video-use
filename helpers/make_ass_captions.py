"""Generate a master.ass subtitle file with per-word color highlighting.

Matches the reel house style:
- Lato Black (weight 900) for base words
- Libre Baskerville Italic for emphasis words
- 78px, bottom-center alignment, double-shadow for legibility
- White (inactive) → gold #FFD86B (active) for base words
- Lavender #E3D7FF (inactive) → purple #C9B6FF (active) for emphasis
- TikTok-style page grouping (≤1100ms per page)
- Per-page Dialogue event with \\t() color transforms per token

Output-timeline offsets applied per the EDL so captions land on the cut.

Usage:
    python helpers/make_ass_captions.py --edl <edl.json> -o <master.ass>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PLAY_RES_X = 1080
PLAY_RES_Y = 1920
FONT_BASE = "Lato Black"
FONT_EMPHASIS = "Libre Baskerville"
FONT_SIZE = 78
MARGIN_L = 60
MARGIN_R = 60
MARGIN_V = 260

# ASS uses &HBBGGRR& (BGR + alpha 00 implicit)
COLOR_INACTIVE = "&HFFFFFF&"
COLOR_ACTIVE = "&H6BD8FF&"
COLOR_EMPHASIS_INACTIVE = "&HFFD7E3&"
COLOR_EMPHASIS_ACTIVE = "&HFFB6C9&"

SWITCH_CAPTIONS_EVERY_MS = 1100

UPPERCASE = False
MAX_WORDS_PER_PAGE = None

WORD_CORRECTIONS = {
    "wicks": "Wix",
    "1st": "first",
    "scaleway": "Scaleway",
    "webcode": "vibe code",
}

MERGE_PHRASES: list[list[str]] = [
    ["vibe", "coding"],
    ["vibe", "code"],
    ["coding", "agent"],
    ["static", "website"],
    ["classical", "artwork"],
    ["custom", "website"],
]

EMPHASIS_WORDS = {
    "geocities", "neopets", "myspace", "html", "css",
    "vibe", "coding", "code",
    "wix", "squarespace", "wordpress",
    "agent", "scaleway", "claude",
    "vision", "brand",
    "classical", "artwork", "modern",
    "marry", "old", "new",
    "creativity", "human", "custom", "first",
}


def clean_word(s: str) -> str:
    return re.sub(r"[^a-zA-Z']", "", s).lower()


def apply_correction(text: str) -> str:
    key = clean_word(text)
    if key in WORD_CORRECTIONS:
        lead = re.match(r"^\s*", text).group(0)
        punct_match = re.search(r"[.,?!]+$", text)
        punct = punct_match.group(0) if punct_match else ""
        return lead + WORD_CORRECTIONS[key] + punct
    return text


def is_emphasis(text: str) -> bool:
    parts = re.split(r"\s+", text)
    return any(clean_word(p) in EMPHASIS_WORDS for p in parts if p)


def merge_phrases(tokens: list[dict]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(tokens):
        merged = False
        for phrase in MERGE_PHRASES:
            if i + len(phrase) > len(tokens):
                continue
            window = tokens[i : i + len(phrase)]
            if all(clean_word(t["text"]) == phrase[idx] for idx, t in enumerate(window)):
                out.append({
                    "text": " ".join(t["text"].strip() for t in window),
                    "startMs": window[0]["startMs"],
                    "endMs": window[-1]["endMs"],
                })
                i += len(phrase)
                merged = True
                break
        if not merged:
            out.append(tokens[i])
            i += 1
    return out


def group_pages(tokens: list[dict]) -> list[list[dict]]:
    pages: list[list[dict]] = []
    cur: list[dict] = []
    cur_start: int | None = None
    for t in tokens:
        if cur_start is None:
            cur_start = t["startMs"]
        full = MAX_WORDS_PER_PAGE is not None and len(cur) >= MAX_WORDS_PER_PAGE
        if cur and (t["endMs"] - cur_start > SWITCH_CAPTIONS_EVERY_MS or full):
            pages.append(cur)
            cur = []
            cur_start = t["startMs"]
        cur.append(t)
    if cur:
        pages.append(cur)
    return pages


def ass_time(ms: int) -> str:
    cs = ms // 10
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")


def hex_to_ass(hex_color: str) -> str:
    """'#RRGGBB' → ASS '&HBBGGRR&'."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}&".upper()


def apply_style(style: dict) -> None:
    """Override module style constants from a style.json dict. Unknown keys ignored."""
    g = globals()
    direct = {
        "font_base": "FONT_BASE", "font_emphasis": "FONT_EMPHASIS",
        "font_size": "FONT_SIZE", "margin_v": "MARGIN_V",
        "play_res_x": "PLAY_RES_X", "play_res_y": "PLAY_RES_Y",
        "switch_captions_every_ms": "SWITCH_CAPTIONS_EVERY_MS",
        "uppercase": "UPPERCASE", "max_words_per_page": "MAX_WORDS_PER_PAGE",
        "word_corrections": "WORD_CORRECTIONS", "merge_phrases": "MERGE_PHRASES",
    }
    for key, const in direct.items():
        if key in style:
            g[const] = style[key]
    colors = {
        "color_inactive": "COLOR_INACTIVE", "color_active": "COLOR_ACTIVE",
        "color_emphasis_inactive": "COLOR_EMPHASIS_INACTIVE",
        "color_emphasis_active": "COLOR_EMPHASIS_ACTIVE",
    }
    for key, const in colors.items():
        if key in style:
            g[const] = hex_to_ass(style[key])
    if "emphasis_words" in style:
        g["EMPHASIS_WORDS"] = set(style["emphasis_words"])


def build_dialogue(page: list[dict]) -> str:
    page_start = page[0]["startMs"]
    page_end = page[-1]["endMs"]
    parts: list[str] = []
    for i, tok in enumerate(page):
        text = tok["text"].strip()
        if not text:
            continue
        if UPPERCASE:
            text = text.upper()
        emph = is_emphasis(text)
        from_ms = max(0, tok["startMs"] - page_start)
        to_ms = max(0, tok["endMs"] - page_start)
        if emph:
            inactive, active = COLOR_EMPHASIS_INACTIVE, COLOR_EMPHASIS_ACTIVE
            font_tag = rf"\fn{FONT_EMPHASIS}\i1"
        else:
            inactive, active = COLOR_INACTIVE, COLOR_ACTIVE
            font_tag = rf"\fn{FONT_BASE}\i0"
        override = (
            f"{{{font_tag}\\1c{inactive}"
            f"\\t({from_ms},{from_ms},\\1c{active})"
            f"\\t({to_ms},{to_ms},\\1c{inactive})}}"
        )
        sep = " " if i > 0 else ""
        parts.append(sep + override + escape_ass(text))
    return f"Dialogue: 0,{ass_time(page_start)},{ass_time(page_end)},Default,,0,0,0,,{''.join(parts)}"


def ass_header() -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_BASE},{FONT_SIZE},{COLOR_INACTIVE},&H00FFFFFF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,3,4,2,{MARGIN_L},{MARGIN_R},{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def words_for_edl(edl: dict, transcripts_dir: Path) -> list[dict]:
    out: list[dict] = []
    seg_offset_ms = 0
    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_dur_ms = int(round((seg_end - seg_start) * 1000))
        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            seg_offset_ms += seg_dur_ms
            continue
        tr = json.loads(tr_path.read_text())
        for w in tr.get("words", []):
            if w.get("type") != "word":
                continue
            ws = w.get("start")
            we = w.get("end")
            if ws is None or we is None:
                continue
            if we <= seg_start or ws >= seg_end:
                continue
            local_start = max(seg_start, ws) - seg_start
            local_end = min(seg_end, we) - seg_start
            text = apply_correction((w.get("text") or "").strip())
            if not text:
                continue
            out.append({
                "text": text,
                "startMs": int(round(local_start * 1000)) + seg_offset_ms,
                "endMs": int(round(local_end * 1000)) + seg_offset_ms,
            })
        seg_offset_ms += seg_dur_ms
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate master.ass with house-style per-word captions")
    ap.add_argument("--edl", type=Path, required=True)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--style", type=Path, default=None,
                    help="Optional style.json overriding fonts/colors/word lists")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")
    edl = json.loads(edl_path.read_text())
    transcripts_dir = edl_path.parent / "transcripts"

    if args.style:
        apply_style(json.loads(args.style.resolve().read_text()))

    tokens = words_for_edl(edl, transcripts_dir)
    tokens = merge_phrases(tokens)
    pages = group_pages(tokens)

    parts = [ass_header()]
    for p in pages:
        parts.append(build_dialogue(p))
    args.output.write_text("\n".join(parts) + "\n")
    total_tokens = sum(len(p) for p in pages)
    print(f"wrote {args.output} — {len(pages)} pages, {total_tokens} tokens")


if __name__ == "__main__":
    main()

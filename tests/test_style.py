"""Self-check for make_ass_captions.py --style. Run: uv run python tests/test_style.py"""
import json, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "helpers"))
import make_ass_captions as mac

# unit: hex → ASS BGR
assert mac.hex_to_ass("#FF0000") == "&H0000FF&"
assert mac.hex_to_ass("#FFD86B") == "&H6BD8FF&"

# integration: run the script with a style file over a synthetic EDL/transcript
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "transcripts").mkdir()
    (tmp / "transcripts" / "clip.json").write_text(json.dumps({
        "words": [
            {"type": "word", "text": "hello", "start": 0.0, "end": 0.4},
            {"type": "word", "text": "world", "start": 0.5, "end": 0.9},
        ]
    }))
    (tmp / "edl.json").write_text(json.dumps({
        "ranges": [{"source": "clip", "start": 0.0, "end": 1.0}]
    }))
    (tmp / "style.json").write_text(json.dumps({
        "font_base": "TestFont",
        "color_active": "#FF0000",
        "uppercase": True,
        "whisper_model": "small",   # agent-only key, must be ignored
    }))
    out = tmp / "master.ass"
    subprocess.run(
        [sys.executable, str(REPO / "helpers" / "make_ass_captions.py"),
         "--edl", str(tmp / "edl.json"), "-o", str(out),
         "--style", str(tmp / "style.json")],
        check=True,
    )
    ass = out.read_text()
    assert "TestFont" in ass, "font_base not applied"
    assert "&H0000FF&" in ass, "color_active hex not converted/applied"
    assert "HELLO" in ass, "uppercase not applied"

    # without --style: defaults untouched
    out2 = tmp / "master2.ass"
    subprocess.run(
        [sys.executable, str(REPO / "helpers" / "make_ass_captions.py"),
         "--edl", str(tmp / "edl.json"), "-o", str(out2)],
        check=True,
    )
    ass2 = out2.read_text()
    assert "Lato Black" in ass2 and "hello" in ass2, "default behaviour changed"

print("test_style.py OK")

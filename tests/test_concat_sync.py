"""Self-check: many-segment concat keeps audio and video the same length (no A/V drift).
Run: uv run python tests/test_concat_sync.py

Regression for the AAC-priming + frame-rounding drift (+0.36 s over 23 segments on a real reel).
"""
import subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "helpers"))
import render

def dur(f, stream):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "stream=duration", "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    return float(out.stdout.strip().split(",")[0])

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    src = tmp / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    # 20 ranges with awkward, non-frame-aligned lengths
    ranges = [{"source": "s", "start": round(i * 1.37 + 0.011, 3), "end": round(i * 1.37 + 0.011 + 0.917 + (i % 3) * 0.05, 3)}
              for i in range(20)]
    edl = {"sources": {"s": str(src)}, "ranges": ranges, "grade": "none"}
    segs = render.extract_all_segments(edl, tmp, preview=False, draft=True, fps=30)
    for s in segs:
        assert abs(dur(s, "v:0") - dur(s, "a:0")) < 0.001, (s.name, dur(s, "v:0"), dur(s, "a:0"))
    base = tmp / "base.mov"
    render.concat_segments(segs, base, tmp)
    drift = dur(base, "a:0") - dur(base, "v:0")
    assert abs(drift) < 0.005, f"A/V drift after concat: {drift * 1000:.1f} ms"

print("ok: per-segment v==a, concat drift < 5 ms")

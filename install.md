---
name: video-use-install
description: Install video-use into the current agent (Claude Code, Codex, etc.), verify dependencies, pre-download the local Whisper model, and set up the user's caption style. No API key required.
---

# video-use install

Use this file only for first-time install or reconnect. For daily editing, read `SKILL.md`. Always read `helpers/` — that's where the scripts live.

## What you're doing

You're setting up a conversation-driven video editor for the user. After install, the user drops raw footage into any folder, runs their agent (`claude`, `codex`, etc.) there, and says "edit these into a launch video." You do the rest by reading `SKILL.md`.

Three things must exist on this machine:

1. The `video-use` repo cloned somewhere stable.
2. `ffmpeg` on `$PATH` (plus optional `yt-dlp` for online sources).
3. The local Whisper model downloaded (faster-whisper — installed by uv sync).

And one thing must be true about the current agent:

4. It can discover `SKILL.md` — either via a global skills directory (`~/.claude/skills/`, `~/.codex/skills/`) or via a `CLAUDE.md` / system-prompt import.

## Install prompt contract

- Do everything yourself. Only ask the user for things you cannot generate — the Whisper model size choice, caption style preferences, an optional ElevenLabs API key if they want diarization, and confirmation before `brew install`.
- Prefer a stable clone path like `~/Developer/video-use` (not `/tmp`, not `~/Downloads`).
- The skill references helpers by bare name (`transcribe.py`, `render.py`). That works because SKILL.md and `helpers/` ship together — keep them as siblings when you register the skill.
- After install, verify by running one real command against one real file. Don't declare success on file-existence checks alone.

## Steps

### 1. Clone to a stable path

```bash
test -d ~/Developer/video-use || git clone https://github.com/browser-use/video-use ~/Developer/video-use
cd ~/Developer/video-use
```

If the repo is already there, `git pull --ff-only` and continue.

### 2. Install Python deps

```bash
# Prefer uv if available; fall back to pip.
command -v uv >/dev/null && uv sync -p 3.12 || pip install -e .
```

If uv picks an unsupported Python (3.15+), pass an explicit interpreter: `uv sync -p 3.12`.

`pyproject.toml` lists `requests`, `librosa`, `matplotlib`, `pillow`, `numpy`, `faster-whisper`. No console scripts — helpers are invoked directly as `python helpers/<name>.py`.

### 3. Install ffmpeg (+ optional yt-dlp)

`ffmpeg` and `ffprobe` are hard requirements. `yt-dlp` is only needed if the user wants to pull sources from URLs. Animation engines such as HyperFrames, Remotion, and Manim are installed lazily the first time a project actually needs them.

```bash
# macOS
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v yt-dlp >/dev/null || brew install yt-dlp     # optional

# Debian / Ubuntu
# sudo apt-get update && sudo apt-get install -y ffmpeg
# pip install yt-dlp

# Arch
# sudo pacman -S ffmpeg yt-dlp
```

If `brew` / `apt` / `pacman` requires a sudo prompt, tell the user the exact command and wait. Do not invent a password.

### 4. Register the skill with the current agent

Figure out which agent you are running under, and register once. A symlink of the whole repo directory is the right shape — helpers/ needs to sit next to SKILL.md.

- **Claude Code** (`~/.claude/` present):

    ```bash
    mkdir -p ~/.claude/skills
    ln -sfn ~/Developer/video-use ~/.claude/skills/video-use
    ```

- **Codex** (`$CODEX_HOME` set, or `~/.codex/` present):

    ```bash
    mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
    ln -sfn ~/Developer/video-use "${CODEX_HOME:-$HOME/.codex}/skills/video-use"
    ```

- **Hermes / Openclaw / another agent with a skills directory**: symlink `~/Developer/video-use` into that agent's skills directory under the name `video-use`. If the agent has no skills directory, add a line to its system prompt / config pointing at `~/Developer/video-use/SKILL.md` (e.g. an `@~/Developer/video-use/SKILL.md` import in a `CLAUDE.md`-equivalent).

If you can't tell which agent you're in, ask the user once: "which agent am I running under — Claude Code, Codex, or something else?" Then pick the right target.

### 5. Local Whisper model — choose and pre-download

Transcription runs locally via faster-whisper (`helpers/transcribe_local.py`). No API key, nothing leaves the machine.

Pick a model size with the user (one question):

- **large-v3** (~3 GB) — best accuracy. Recommended on Apple Silicon or any recent multi-core CPU.
- **medium / small** — noticeably faster on older or low-RAM machines; accuracy still fine for clean speech.

Pre-download it now so the user's first real transcription isn't a multi-gigabyte surprise:

```bash
uv run python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')"
```

(Substitute the chosen size.) Record the choice as `whisper_model` in `style.json` (step 6).

**Optional — ElevenLabs Scribe.** For speaker diarization and audio-event tags (`(laughs)`, `(applause)`), the cloud path in `helpers/transcribe.py` still works: put `ELEVENLABS_API_KEY=...` in `.env` at the repo root (`chmod 600`, never commit it) and use `transcribe.py` instead of `transcribe_local.py`. Skip this unless the user asks for diarization.

### 6. Caption style wizard

Ask the user, one question at a time:

1. **Caption font** (bold sans like "Lato Black" is the house default).
2. **Emphasis font** (italic serif like "Libre Baskerville"; "same as base" is fine).
3. **Two colours**: active word colour + emphasis colour, as `#RRGGBB` hex.
4. **Caption mode**: 2-word UPPERCASE chunks (`"uppercase": true, "max_words_per_page": 2`) or phrase-grouped (`"uppercase": false, "max_words_per_page": null`).

Then:

1. **Verify the fonts exist** — matplotlib ships with the repo deps:

    ```bash
    uv run python -c "
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    for want in ['Lato Black', 'Libre Baskerville']:   # substitute user's choices
        print(want, '->', 'OK' if want in names else 'MISSING')
    "
    ```

    If a font is missing, say so and list the closest installed names (`sorted(n for n in names if want.split()[0].lower() in n.lower())`) before writing anything.

2. **Write `style.json`** into the user's videos directory: copy `style.example.json`, apply their answers, delete the `_comment` key or leave it — it's ignored.

3. **Render a 3-second sample** from any short clip the user has (or a generated test card):

    ```bash
    # black test card with a burned caption, if no clip is at hand
    ffmpeg -y -f lavfi -i color=c=black:s=1080x1920:d=3 -f lavfi -i anullsrc=r=48000:cl=mono -shortest sample_base.mp4
    ```

    Build a tiny EDL + transcript JSON for two words, run `make_ass_captions.py --style style.json`, burn with `ffmpeg -vf "ass=master.ass"`, and show the user the result. Iterate until they approve, then keep `style.json` as approved.

### 7. Verify end-to-end

Run one real thing. Prefer the lightest verification that still proves the pipeline is wired up:

```bash
python ~/Developer/video-use/helpers/timeline_view.py --help >/dev/null && echo "helpers OK"
ffprobe -version | head -1
```

Full transcription test is cheap now — it's local. If the user has any short clip handy, run `uv run python helpers/transcribe_local.py <clip> --model <chosen>` as the real verification.

### 8. Hand off

Tell the user, in one short message:

- Where the skill is installed (`~/Developer/video-use`).
- That they should `cd` into their footage folder and start their agent there (e.g. `claude`).
- That a good first message is: *"edit these into a launch video"* or *"inventory these takes and propose a strategy."*
- That all outputs land in `<videos_dir>/edit/` — the repo stays clean.

## Keeping the skill current

- `cd ~/Developer/video-use && git pull --ff-only` pulls the latest code. The symlink auto-picks it up on the next run.
- If `pyproject.toml` changed deps, re-run `uv sync` / `pip install -e .` after pulling.

## Cold-start reminders

- Symlink the **whole directory**, not just `SKILL.md`. The helpers need to sit next to it.
- If `.env` exists but the key is empty, treat it the same as missing — don't assume existence means validity.
- `ffmpeg` from static builds works fine. Any modern (≥ 4.x) build is enough.
- `yt-dlp` is optional. Don't block install on it; install lazily the first time a user asks to pull from a URL.
- Node.js/npm are only needed for HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- HyperFrames, Remotion, and Manim are optional animation engines. Don't install or prefer one globally during setup; pick the engine per animation slot in `SKILL.md`. HyperFrames can run through `npx --yes hyperframes ...` in the slot directory. Remotion can be scaffolded with `npx create-video@latest` or installed inside the slot before rendering.
- Local transcription is free — verify with a real clip when one is available. Only the optional ElevenLabs path costs money.
- If the user is on Linux without a package manager Claude recognizes, print the manual `ffmpeg` install URL and wait rather than guessing.

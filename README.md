# gta-shorts

Find the moments in GTA VI Twitch VODs that viewers clipped, download only
those stretches, and turn them into vertical shorts with a self-hosted,
lightly patched [openshorts](https://github.com/mutonby/openshorts).

```
Twitch API ──> viewer clips ──> hot stretches (12 min each)
                                   │  yt-dlp, only that range (~65 MB/min)
                                   ▼
                     openshorts (GPU, Docker): transcribe → Gemini picks moments
                     → 9:16 facecam layout → captions + hook
                                   │
                                   ▼
             runs/<date>_<streamer>_<vod>/manifest.json + clipNN.mp4
             (captions with streamer credit, VOD timestamps, scores)
```

Phase 1 (this) = run it by hand on real VODs and tune quality. Discovery on a
schedule, posting and the Discord digest come in phases 2-3.

## What is patched in openshorts

Pinned to `deploy/openshorts.pin`; `scripts/setup_openshorts.sh` clones it and
applies `patches/`. Upstream's own test suite passes with both patches.

1. **Clip profile** (`0001`): `CLIP_PROFILE_FILE` adds deployment guidance to
   the prompts openshorts actually uses (score, detail, vision). Ours is
   `deploy/gta6_profile.txt`: what a good GTA moment is, what to skip, hook
   and hashtag rules, no streamer names (we add the credit ourselves).
2. **VOD reference for uploads** (`0002`): self-hosted openshorts silently
   ignores `source_start`/`source_end` and would download a whole 12-47 h VOD.
   We download the stretch ourselves and upload it; this patch adds
   `twitch_vod_url` + `twitch_vod_offset` to `/api/process` so the job still
   gets the viewer-clip "heat" signal (the strongest hint for loud,
   low-dialogue GTA moments).

## Setup on the Windows GPU machine

1. **Windows side**
   - Current NVIDIA driver (Game Ready or Studio). Do not install a Linux
     NVIDIA driver inside WSL.
   - PowerShell (admin): `wsl --install -d Ubuntu-24.04`, reboot, create the user.
   - Docker Desktop: Settings → General → *Use the WSL 2 based engine*;
     Resources → WSL integration → enable *Ubuntu-24.04*.
   - Windows power settings: never sleep (it runs 24/7 from phase 2 on).
2. **Inside Ubuntu (WSL)**. Keep the project in the Linux filesystem
   (`~/gta`), not under `/mnt/c`: bind mounts from `/mnt/c` are very slow.
   ```bash
   sudo apt update && sudo apt install -y git ffmpeg
   curl -LsSf https://astral.sh/uv/install.sh | sh && exec $SHELL
   docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   # must list the GPU
   # get this project onto the machine (private git remote, or copy the folder)
   cd ~/gta && uv sync
   ```
3. **Keys**
   - Twitch app: https://dev.twitch.tv/console → Register application
     (category *Other*, redirect `http://localhost`, client type
     *Confidential*). Copy the client ID and generate a secret.
   - Gemini key: https://aistudio.google.com/app/apikey
   ```bash
   cp .env.example .env                    # TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
   cp config.example.toml config.toml
   scripts/setup_openshorts.sh --gpu       # clones, patches, writes openshorts/.env
   nano openshorts/.env                    # GEMINI_API_KEY + the same two Twitch values
   ```
4. **Start openshorts** (first build ~10-20 min; models download on the first job)
   ```bash
   cd openshorts && docker compose up --build -d backend frontend && cd ..
   docker exec openshorts-backend nvidia-smi -L                       # GPU visible
   docker exec openshorts-backend ffmpeg -hide_banner -f lavfi -i testsrc=size=256x256:rate=1 \
     -frames:v 1 -c:v h264_nvenc -f null -                            # NVENC works
   uv run gta-shorts doctor                                           # all [ok]
   ```
   (`renderer` is only for the dashboard's Remotion editor; the clip
   pipeline doesn't need it.) Dashboard: http://localhost:5175.

## Phase 1: clip real VODs by hand

```bash
uv run gta-shorts discover                    # who is streaming GTA VI right now
uv run gta-shorts vods xqc                    # a channel's VODs + viewer-clip counts
uv run gta-shorts plan 2935383720             # which stretches would be clipped
uv run gta-shorts clip 2935383720             # download + clip them (resumable)
uv run gta-shorts clip 2935383720 --start 2:00:00 --end 2:12:00   # pick by hand
```

Output: `runs/<date>_<streamer>_<vod>/` with `stretch_*/clipNN.mp4` and
`manifest.json` (per clip: score, hook, why, captions with the credit line,
`vod_link` to the exact moment on Twitch, raw openshorts metadata incl. Gemini
cost). Interrupted? Re-run the same command; it resumes.

### What to judge on the first 2-3 VODs

| Check | Knob if it's wrong |
|---|---|
| Facecam stacked under gameplay, not a blurred crop | `layouts` in config.toml (`["auto"]` lets Gemini pick) |
| Clips open on the action/reaction, end after the payoff | `[detail]` in `deploy/gta6_profile.txt` |
| The picked moments are the ones you'd post | `[score]` in the profile; `stretch_minutes` / `min_viewer_clips` |
| Hooks name what happens, no generic filler | hook rules in `[detail]` |
| Captions readable, not covering the facecam | dashboard caption styles; tell me what you see |
| Time per 12-min stretch on the GPU, Gemini cost | `TRANSCRIBE_BACKEND=parakeet` in openshorts/.env |

After editing the profile: `cp deploy/gta6_profile.txt openshorts/profiles/gta6.txt`.
Each job reads it fresh, so no restart is needed.

## Upkeep

- Twitch changes break yt-dlp now and then: `uv lock --upgrade-package yt-dlp && uv sync`.
- Move to a newer openshorts: put the new commit in `deploy/openshorts.pin`, run
  `scripts/setup_openshorts.sh --gpu`, rebuild. A patch that no longer applies
  fails loudly.
- Tests: `uv run pytest` (this project). For the patched openshorts, upstream's
  suite runs with their CI dependency list (see `.github/workflows/ci.yml` in it).

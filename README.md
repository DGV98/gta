# gta-shorts

Find the moments in GTA VI Twitch VODs that viewers clipped, download only
those stretches, and turn them into vertical shorts with a self-hosted,
lightly patched [openshorts](https://github.com/mutonby/openshorts).

```
watcher (every 30 min): top GTA VI streams + your priority channels
   │  a stream ended 30+ min ago → its GTA VI viewer clips (unmuted parts only)
   ▼
hot stretches (12 min each) ──> queue in runs/state.db (priority first, then views,
   │                                within a daily stretch + Gemini $ cap)
   │  only the HLS segments covering that range (~60 MB/min)
   ▼
openshorts (GPU, Docker): transcribe → Gemini picks moments
   → 9:16: gameplay on top, webcam below → captions + hook
   │
   ▼
runs/<date>_<streamer>_<vod>/manifest.json + clipNN.mp4
(captions with streamer credit, VOD timestamps, scores; clips listed in state.db)
   │
   ▼
poster: the best clips ──> runs/to_post/ + captions, you upload them by hand
                       └─> or Upload-Post posts them to TikTok for you
```

- Phase 1: clip VODs by hand and tune quality (`plan`, `clip`).
- Phase 2: the watcher does it 24/7 (`scan`, `watch`, `status`).
- Phase 3: the poster gets the best clips onto TikTok: in a folder for you to
  upload, or through Upload-Post (`posts`, `post`, `skip`). Still to do: the
  daily Discord digest.

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
5. **Survive reboots** (for phase 2's 24/7 watcher)
   - Docker Desktop → Settings → General → *Start Docker Desktop when you sign
     in*. Both openshorts and the watcher use `restart: unless-stopped`, so
     they come back when Docker does.
   - Windows signs in by itself after a reboot: `netplwiz` → untick *Users
     must enter a user name and password* (or use Sysinternals Autologon).
   - Windows Update → Advanced options: set active hours / pause updates so
     the machine isn't restarted mid-job (a restart is safe, just slow:
     everything resumes).
   - Test it once: reboot, wait for Docker Desktop, then `docker ps` should
     list `openshorts-backend`, `gta-shorts-watcher` and `gta-shorts-poster`.

## Phase 1: clip real VODs by hand

```bash
uv run gta-shorts discover                    # who is streaming GTA VI right now
uv run gta-shorts vods xqc                    # a channel's VODs + viewer-clip counts
uv run gta-shorts plan 2935383720             # which stretches would be clipped
uv run gta-shorts clip 2935383720             # download + clip them (resumable)
uv run gta-shorts clip 2935383720 --start 2:00:00 --end 2:12:00   # pick by hand
```

`plan`/`clip` only count viewer clips cut while the stream was in GTA VI, and
skip parts Twitch muted for copyrighted music (no audio = no transcript).

Output: `runs/<date>_<streamer>_<vod>/` with `stretch_*/clipNN.mp4` and
`manifest.json` (per clip: score, hook, why, captions with the credit line,
`vod_link` to the exact moment on Twitch, raw openshorts metadata incl. Gemini
cost). Interrupted? Re-run the same command; it resumes.

### What to judge on the first 2-3 VODs

| Check | Knob if it's wrong |
|---|---|
| Facecam stacked under gameplay, not a blurred crop | `layouts = ["screencast"]` in config.toml (`["auto"]` crops gameplay through a side facecam) |
| Clips open on the action/reaction, end after the payoff | `[detail]` in `deploy/gta6_profile.txt` |
| The picked moments are the ones you'd post | `[score]` in the profile; `stretch_minutes` / `min_viewer_clips` |
| Hooks name what happens, no generic filler | hook rules in `[detail]` |
| Captions readable, not covering the facecam | dashboard caption styles; tell me what you see |
| Time per 12-min stretch on the GPU, Gemini cost | `TRANSCRIBE_BACKEND=parakeet`, `GEMINI_MODEL` in openshorts/.env |

After editing the profile or openshorts/.env, run `scripts/up.sh`: the backend
runs the copy of the profile built into its image, so it needs the rebuild.

`HOOK_GROUNDING=0` (in openshorts/.env) keeps the hooks the clip picker writes
under the profile's rules. With it on, openshorts rewrites the hook of every
stacked clip from three frames, with a prompt meant for screen recordings: on a
GTA stream that turned "Chat drama put a hit on him 💀" into "Checking Randy's
location on the GTA map menu after 3,000".

## Phase 2: the 24/7 watcher

Every `poll_minutes` it scans Twitch, then works its queue one stretch at a
time until the next scan:

1. Tracks channels streaming GTA VI in English with at least `min_viewers`
   (from the top `discover_top` streams), plus every `priority` channel
   whatever it plays. A discovered channel is dropped after `track_days`
   without GTA VI.
2. Looks at each tracked channel's last 3 VODs. A VOD whose stream is still
   live waits. Once the stream ended `settle_minutes` ago, its GTA VI viewer
   clips are fetched and its hot stretches queued. Skipped and recorded with a
   reason: VODs that ended more than `max_vod_age_hours` ago and VODs with no
   (unmuted) GTA VI viewer clips. Subscriber-only VODs are dropped at their
   first download attempt (Twitch's API doesn't flag them).
3. Queue order: a job already running in openshorts, then priority channels,
   then the most-viewed viewer clips. New jobs stop for the rolling 24 h once
   `max_stretches_per_day` or `max_usd_per_day` (openshorts' own Gemini cost
   figure) is reached. It also waits while openshorts is down or the disk has
   less than `min_free_gb` free.
4. A failed stretch is retried after 20 and then 40 min (from the kept download),
   and marked failed after `max_attempts`. A job lost because openshorts
   restarted is resubmitted.
5. Finished clips go into `runs/state.db` (table `clips`, with a `posted_at`
   column for phase 3). Clip files older than `keep_clip_days` are deleted;
   manifests stay.

Settings are in the `[watch]` section of `config.toml` (see
`config.example.toml`). Put your clipping-program streamers in `priority`.

```bash
uv run gta-shorts scan            # one scan, no clipping: see what it tracks and would queue
uv run gta-shorts watch --once    # scan, clip whatever is ready, exit (a first real test)
scripts/up.sh                     # start openshorts + the watcher container, 24/7
docker compose logs -f watcher    # what it is doing
uv run gta-shorts status          # tracked channels, queue, last 24 h: jobs, $, clips, failures
```

`scripts/up.sh` is also how you apply changes: after a `git pull`, a
`config.toml` edit or a profile edit, run it again (it restarts the watcher
and poster, since they read `config.toml` only at start). Stop the watcher with
`docker compose stop watcher`. All state is in `runs/`, so stopping, crashing
or rebooting loses nothing: a running job is polled again, and a half-done
stretch carries on.

The first scan queues every hot stretch from the last `max_vod_age_hours`
of all tracked channels. The daily cap spreads them out, and stretches that
go stale before their turn are dropped.

## Phase 3: posting to TikTok

The poster container (`gta-shorts post`) picks the best finished clips. In
the same order for both modes: priority channels first, then a different
streamer than the last one, then openshorts' score. It only takes clips with
a score of at least `min_score`, made in the last `max_age_hours`, and at most
`max_per_vod` from one stream. With `priority_only = true` it takes only your
`[watch] priority` channels. `[post] mode` says what happens to them:

- `folder` (the default): you upload them by hand.
- `upload-post`: it posts them for you through Upload-Post (paid).
- `off`: neither.

The caption is the clip's `captions.tiktok` in its `manifest.json`
(openshorts' description, then the streamer credit). `uv run gta-shorts posts`
shows what's next and what went out. `uv run gta-shorts skip <clip>` keeps one
off TikTok.

### Folder mode: upload by hand

`runs/to_post/` always holds the next `folder_size` clips (6 by default),
numbered in the order they came in. Each has a `.txt` with the caption to paste.

1. Open the folder from Ubuntu with `explorer.exe "$(wslpath -w runs/to_post)"`,
   and pin it to *Quick access* in File Explorer.
2. Upload a clip at https://www.tiktok.com/upload (or from your phone), and
   paste the caption from its `.txt`.
3. Delete the clip from the folder, or move it somewhere else. It now counts as
   posted, and within a minute the next clip takes its place. Its `.txt` goes too.

To drop a clip without counting it as posted, use
`uv run gta-shorts skip 0003_name.mp4`. A clip left in the folder longer than
`max_age_hours` is swapped for a newer one. `uv run gta-shorts post --once`
tops the folder up right away, without the container.

### Upload-Post mode: fully automatic

TikTok only lets apps it has audited post publicly, and its guidelines rule
out auditing "a utility tool to help upload contents to the account(s) you or
your team manages". An app of our own could only post private videos. So
this mode goes through [Upload-Post](https://upload-post.com), whose TikTok app
is audited. TikTok isn't on its free plan: Basic is $24/month ($16 billed
yearly). TikTok also caps posts made through any app at about 15 per account
per day.

1. On app.upload-post.com: pick a paid plan, then *Manage Users* → create a
   profile (e.g. `gta`) and connect your TikTok account to it. Then *API Keys*
   → *Generate New API Key*.
2. `.env`: `UPLOAD_POST_API_KEY=...`. `config.toml`, section `[post]`:
   `mode = "upload-post"` and `user = "gta"` (the profile name, not your TikTok
   handle). The rest of `[post]` is the schedule; see `config.example.toml`.
3. `uv run gta-shorts doctor`: the Upload-Post lines say `[ok]`.
4. `uv run gta-shorts post --once --private` posts the top clip now as
   *only me*, so you can check how it looks on TikTok.
5. `scripts/up.sh`, then follow it with `docker compose logs -f poster`.

How it works:

- **Pace:** `per_day` posts per rolling 24 h, at least `min_gap_minutes`
  apart, only between `hours` in `timezone`.
- **Failures:** a failed post is retried after 30 and then 60 min, then marked
  failed. A file TikTok rejects is not retried. Account problems pause all
  posting for an hour and don't use up the clip's attempts: a bad key, the free
  plan, a daily cap, or TikTok needing to be reconnected. `posts` and `status`
  show the reason.
- **Never twice:** each attempt has its own Upload-Post request id, saved
  before the upload. After a crash, restart or timeout, the poster asks
  Upload-Post about that request instead of uploading again.
- **Inbox:** rarely, TikTok puts the video in your TikTok inbox as a draft
  instead of publishing it (status `inbox` in `posts`). Publish it from the
  TikTok app.

Switching modes or stopping: change `mode` and run `scripts/up.sh` (a post
already under way still finishes), or `docker compose stop poster` for a quick
pause.

## Upkeep

- Twitch changes break yt-dlp now and then: `uv lock --upgrade-package yt-dlp && uv sync`.
- Move to a newer openshorts: put the new commit in `deploy/openshorts.pin`, run
  `scripts/setup_openshorts.sh --gpu`, rebuild. A patch that no longer applies
  fails loudly.
- The watcher image pins its yt-dlp from `uv.lock`: after upgrading it, run
  `scripts/up.sh` to rebuild.
- Tests: `uv run pytest` (this project). For the patched openshorts, upstream's
  suite runs with their CI dependency list (see `.github/workflows/ci.yml` in it).

# To do on the Windows PC

Work through these in order. Lines in `code` are commands to paste. Unless a
step says PowerShell, paste them into the **Ubuntu** window. Once the project
is installed (step 3), type `cd ~/gta` every time you open Ubuntu, before
running anything. If a step goes wrong, README.md has more detail.

## 1. Get these ready first

- [ ] **Twitch keys.** Go to https://dev.twitch.tv/console and click *Register
      Your Application*. Use any name, put `http://localhost` as the OAuth
      redirect URL, choose category *Other* and client type *Confidential*.
      Copy the **Client ID**, then click *New Secret* and copy the **Client Secret**.
- [ ] **Gemini key.** Create one at https://aistudio.google.com/app/apikey.
- [ ] **Your streamers.** Write down the Twitch usernames of the streamers
      from your clipping programs.

## 2. Set up Windows

- [ ] Update the NVIDIA graphics driver (GeForce Experience or nvidia.com).
- [ ] Open **PowerShell as administrator** and run `wsl --install -d Ubuntu-24.04`.
      Restart the PC, then pick a username and password when Ubuntu asks.
- [ ] Install **Docker Desktop**, then open its Settings:
  - General: tick *Use the WSL 2 based engine* and *Start Docker Desktop when you sign in*.
  - Resources → WSL integration: turn on *Ubuntu-24.04*.
- [ ] So the PC can run 24/7:
  - Power settings: set *Sleep* to **Never**.
  - Sign in automatically after a restart: press Win+R, type `netplwiz` and
    untick *Users must enter a user name and password*.
  - Windows Update: set your active hours so updates don't restart the PC whenever they like.

## 3. Install the project (in Ubuntu)

Open **Ubuntu** from the Start menu.

- [ ] `sudo apt update && sudo apt install -y git ffmpeg gh`
- [ ] `curl -LsSf https://astral.sh/uv/install.sh | sh`, then close Ubuntu and open it again.
- [ ] Check that Docker can see the graphics card. It should print a table with your card's name:
      `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`
- [ ] Download the code: `gh auth login` (follow the prompts), then
      `gh repo clone <your-github-name>/<repo-name> ~/gta`
- [ ] `cd ~/gta && uv sync`

## 4. Add your keys and settings

To save in nano: Ctrl+O, then Enter, then Ctrl+X.

- [ ] `cp .env.example .env && nano .env`. Paste in the Twitch Client ID and Secret.
- [ ] `cp config.example.toml config.toml && nano config.toml`. Under `[watch]`, add
      your streamers, like this: `priority = ["streamer1", "streamer2"]`.
- [ ] `scripts/setup_openshorts.sh --gpu`
- [ ] `nano openshorts/.env`. Fill in `GEMINI_API_KEY`, `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.

## 5. Try it by hand on 2-3 streams

- [ ] Start openshorts. The first time takes 10-20 minutes:
      `cd openshorts && docker compose up --build -d backend frontend && cd ..`
- [ ] `uv run gta-shorts doctor`. Every line should say `[ok]`.
- [ ] `uv run gta-shorts discover` shows who's streaming GTA VI right now.
- [ ] `uv run gta-shorts vods <streamer>` lists their past streams. Copy one of the ID numbers.
- [ ] `uv run gta-shorts clip <id>` makes the clips.
- [ ] Watch the clips: `explorer.exe runs` opens the folder in Windows.
- [ ] Send me:
  - what looks good or bad in the clips
  - the `manifest.json` files from `runs/`
  - your graphics card model

## 6. Turn on 24/7 mode

- [ ] `uv run gta-shorts scan` shows what it would pick up. It doesn't make anything yet.
- [ ] `scripts/up.sh` starts everything. It keeps running after you close Ubuntu.
- [ ] `docker compose logs -f watcher` shows what it's doing. Press Ctrl+C to stop
      watching; the watcher itself keeps running.
- [ ] `uv run gta-shorts status` shows the queue, what it made and what it spent today. Run it any time.

## 7. Check that it survives a restart

- [ ] Restart Windows and wait a few minutes. Then open Ubuntu and run `docker ps`.
      You should see `openshorts-backend` and `gta-shorts-watcher` listed.
      If they're missing, tell me.

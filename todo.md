# Set up and start posting clips

Setup takes about an hour, mostly waiting for things to install. After that,
the PC finds GTA VI streams by itself, cuts the best moments into clips, and
puts them in a folder for you to post on TikTok.

**How to use this list**

- Do the steps in order and tick them off as you go.
- Text `like this` is a command. Copy it, paste it into the window the step
  names, and press Enter. To paste in Ubuntu, right-click.
- From step 3 on, every time you open Ubuntu, run `cd ~/gta` first.
- Some steps open a text editor called nano. To save and close it, press Ctrl+O,
  then Enter, then Ctrl+X.
- Stuck? Send me what's on the screen (a screenshot is fine).

## 1. Get your keys (in your web browser)

Paste each one into Notepad as you go. You'll need them in step 4.

- [ ] **Twitch keys.** Go to https://dev.twitch.tv/console and click *Register
      Your Application*. If Twitch asks, turn on two-factor authentication first.
      - Name: anything, for example `gta-shorts-` plus your name
      - OAuth Redirect URL: `http://localhost`
      - Category: *Other*. Client type: *Confidential*.
      Then click *Manage* next to the app. Copy the **Client ID**, click
      *New Secret*, and copy the **Client Secret**.
- [ ] **Gemini key.** Go to https://aistudio.google.com/app/apikey, click
      *Create API key*, and copy it.
- [ ] **Your streamers.** Write down the Twitch usernames of the streamers in your
      clipping programs (the part after `twitch.tv/` in their link).

## 2. Install Ubuntu and Docker (on Windows)

- [ ] Right-click the Start button and choose *Terminal (Admin)*. Run:
      `wsl --install -d Ubuntu-24.04`
      Restart the PC if it asks. When the Ubuntu window opens, choose a username
      and password. The password stays invisible while you type, which is normal.
      Don't forget it.
- [ ] Download **Docker Desktop** from https://www.docker.com/products/docker-desktop/
      and install it with the default options. Open it (you can skip signing in),
      then click the gear icon to open Settings:
      - *General*: tick *Use the WSL 2 based engine* and *Start Docker Desktop when
        you sign in to your computer*.
      - *Resources → WSL integration*: switch on *Ubuntu-24.04*.
      - Click *Apply & restart*.

Your graphics card driver is already up to date, so there's nothing to do there.

## 3. Install the project (in Ubuntu)

Open **Ubuntu** from the Start menu.

- [ ] `sudo apt update && sudo apt install -y git ffmpeg gh`
      (it asks for your Ubuntu password)
- [ ] `curl -LsSf https://astral.sh/uv/install.sh | sh`
      Then close Ubuntu and open it again.
- [ ] Check that Docker can use your graphics card:
      `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`
      You should see a table that mentions *RTX 4070 Ti SUPER*. If you get an
      error instead, stop and send it to me.
- [ ] `gh auth login`
      Press Enter to accept each choice. When it shows a code, open
      https://github.com/login/device, type the code, and approve.
- [ ] `gh repo clone DGV98/gta ~/gta && cd ~/gta && uv sync`

## 4. Add your keys (in Ubuntu)

- [ ] `cp .env.example .env && nano .env`
      Paste your Twitch Client ID after `TWITCH_CLIENT_ID=` and the secret after
      `TWITCH_CLIENT_SECRET=`. Leave the other line empty. Save and close.
- [ ] `cp config.example.toml config.toml && nano config.toml`
      Press Ctrl+W, type `priority`, and press Enter to jump to the line
      `priority = []`. Put your streamers in it like this:
      `priority = ["streamer1", "streamer2"]`. Save and close.
      (Optional: to post *only* those streamers, find `priority_only = false`
      further down and change it to `true`.)
- [ ] `scripts/setup_openshorts.sh --gpu`
- [ ] `nano openshorts/.env`
      Fill in `GEMINI_API_KEY=`, `TWITCH_CLIENT_ID=` and `TWITCH_CLIENT_SECRET=`.
      The two Twitch values are the same ones as before. Save and close.

## 5. Start it (in Ubuntu)

- [ ] `scripts/up.sh`
      The first time takes 10-20 minutes. After that it keeps running in the
      background, even after you close Ubuntu.
- [ ] `uv run gta-shorts doctor`
      Every line should say `[ok]`. If the openshorts line says FAIL, wait a few
      minutes and run it again. If anything else says FAIL, send me the output.
- [ ] `uv run gta-shorts status` shows the streams it found and what it's working
      on. Run it any time.

## 6. Post your first clips

Finished clips show up in a folder called `to_post`. The first ones can take an
hour or two to appear.

- [ ] `uv run gta-shorts posts` shows how many clips are in the folder.
- [ ] Open the folder: `explorer.exe "$(wslpath -w runs)"`
      In the window that opens, right-click *to_post* and choose *Pin to Quick
      access*. Next time you can open it straight from File Explorer.
- [ ] For each clip:
      1. Watch it. If you don't like it, just delete it and move on.
      2. Upload it at https://www.tiktok.com/upload (or from your phone).
      3. Open the `.txt` file with the same name, copy all of it, and paste it in
         as the caption. It already credits the streamer.
      4. Delete the clip from the folder. That tells the system it's done, and a
         new clip takes its place within a minute.

A few posts spread across the day usually do better than many at once. The
TikTok uploader also lets you schedule posts.

## 7. Keep it running when you're away

- [ ] Stop the PC from sleeping: Settings → System → Power → set sleep to *Never*.
- [ ] Sign in automatically after a restart: press Win+R, type `netplwiz`, press
      Enter, untick *Users must enter a user name and password to use this
      computer*, click OK, and type your password.
      If that box isn't there: Settings → Accounts → Sign-in options, turn off
      *For improved security, only allow Windows Hello sign-in...*, then try again.
- [ ] Settings → Windows Update → Advanced options → *Active hours*: set them as
      wide as Windows allows, so it restarts less often.
- [ ] Test it: restart Windows, wait 5 minutes, open Ubuntu and run `docker ps`.
      You should see `gta-shorts-watcher`, `gta-shorts-poster` and
      `openshorts-backend`. If one is missing, tell me.

## From now on

- **Every day:** open *to_post*, post the clips you like, and delete each one
  from the folder once it's up.
- **When I send you an update:** in Ubuntu, run
  `cd ~/gta && git pull && scripts/up.sh`.
- **Tell me** which clips were good or bad, and why. That's how I tune what it picks.

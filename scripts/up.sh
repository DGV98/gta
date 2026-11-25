#!/usr/bin/env bash
# Start or update everything: openshorts (backend + dashboard), then the watcher
# and the poster. Safe to re-run after a git pull, a profile edit or a config change.
set -euo pipefail
cd "$(dirname "$0")/.."

for f in .env config.toml openshorts/.env; do
    [[ -f $f ]] || { echo "missing $f (see README: Setup)" >&2; exit 1; }
done
# Docker would create a missing bind-mount dir as root; the watcher runs as you.
mkdir -p runs
cp deploy/gta6_profile.txt openshorts/profiles/gta6.txt

(cd openshorts && docker compose up -d --build backend frontend)

# --force-recreate: config.toml is only read at start, and compose can't see
# edits inside a mounted file. Both services resume where they left off.
UID_GID="$(id -u):$(id -g)" docker compose up -d --build --force-recreate
docker compose ps
echo
echo "Follow it:  docker compose logs -f watcher    (or poster)"
echo "Queue:      uv run gta-shorts status"
echo "TikTok:     uv run gta-shorts posts"

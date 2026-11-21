#!/usr/bin/env bash
# Clone openshorts at the pinned commit, apply our patches and drop in the
# deployment files (GPU compose override, .env, GTA prompt profile).
#
#   scripts/setup_openshorts.sh            # CPU (laptop) or GPU, see --gpu
#   scripts/setup_openshorts.sh --gpu      # add the NVIDIA compose override
#
# Safe to re-run: an existing checkout is reset to the pin and re-patched.
# Your openshorts/.env is never overwritten once it exists.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/openshorts"
REPO_URL="https://github.com/mutonby/openshorts.git"
PIN="$(tr -d '[:space:]' < "$ROOT/deploy/openshorts.pin")"
GPU=0
[[ "${1:-}" == "--gpu" ]] && GPU=1

if [[ ! -d "$DEST/.git" ]]; then
  echo "==> Cloning openshorts into $DEST"
  git clone --filter=blob:none "$REPO_URL" "$DEST"
fi

cd "$DEST"
echo "==> Resetting to pin $PIN"
git fetch --quiet origin "$PIN" 2>/dev/null || git fetch --quiet origin
git checkout --quiet --force "$PIN"
# Drop untracked files from a previous run's patches; keep .env and output/.
git clean -fdq -e .env -e output -e docker-compose.override.yml

echo "==> Applying patches"
for p in "$ROOT"/patches/*.patch; do
  echo "    $(basename "$p")"
  git apply --whitespace=nowarn "$p"
done

mkdir -p profiles
cp "$ROOT/deploy/gta6_profile.txt" profiles/gta6.txt

if [[ ! -f .env ]]; then
  cp "$ROOT/deploy/openshorts.env.example" .env
  echo "==> Wrote openshorts/.env from the template: fill in the keys."
else
  echo "==> Kept existing openshorts/.env"
fi

if [[ "$GPU" == 1 ]]; then
  cp "$ROOT/deploy/docker-compose.override.gpu.yml" docker-compose.override.yml
  echo "==> GPU compose override installed"
else
  rm -f docker-compose.override.yml
fi

echo "==> Done. Start it with:  cd openshorts && docker compose up --build -d"

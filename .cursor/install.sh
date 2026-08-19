#!/usr/bin/env bash
# Cloud Agent install phase for HiveWeave.
# Idempotent: safe to re-run against a warm/snapshotted VM.
# Prepares both the Python/FastAPI backend (uv) and the React/Vite frontend (pnpm).
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- PATH setup (works in login and non-login shells) -----------------------
# uv installs to ~/.local/bin; pnpm/corepack are nvm-managed on the base image.
export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck disable=SC1090,SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true

# --- uv (Python package manager) -------------------------------------------
# The base image may not ship uv; bootstrap it into ~/.local/bin if missing.
if ! command -v uv >/dev/null 2>&1; then
  echo "[install] uv not found — installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

# --- Sanity: node + pnpm must be present -----------------------------------
command -v node >/dev/null 2>&1 || { echo "[install] ERROR: node not found on PATH"; exit 1; }
command -v pnpm >/dev/null 2>&1 || { echo "[install] ERROR: pnpm not found on PATH"; exit 1; }
echo "[install] node $(node -v), pnpm $(pnpm -v)"

# --- Backend deps (apps/hiveweave-py) --------------------------------------
echo "[install] Syncing backend Python deps (uv sync --extra dev)..."
uv sync --extra dev --directory apps/hiveweave-py

# --- Frontend deps (pnpm workspace) ----------------------------------------
echo "[install] Installing frontend deps (pnpm install --frozen-lockfile)..."
pnpm install --frozen-lockfile

echo "[install] Done."

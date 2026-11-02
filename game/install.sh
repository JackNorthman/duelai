#!/bin/bash
# Installs the DUELAI game side into a Bridge Commander (Orion Delta 4.1)
# install:
#   1. scripts/Custom/Autoload/ClaudeCommander.py  (new file, the command link)
#   2. patches/*.patch applied to the game's own scripts (unified diffs,
#      applied with `patch -p1` from the game root)
#
# Usage:   bash game/install.sh "/path/to/Star Trek Bridge Commander"
#          bash game/install.sh "/path/to/Star Trek Bridge Commander" undo
#      or  STBC_GAME_ROOT=/path/to/... bash game/install.sh [undo]
#
# Refuses while stbc.exe is running (the engine holds its scripts open, and a
# half-written script mid-game can crash it).  Every file it touches is backed
# up beside itself as *.pre-duelai.bak first; `undo` puts those back.  Stale
# .pyc files are removed so the game recompiles from the new .py.
# Needs bash, python3 (or python) and GNU patch - Git Bash on Windows has all
# three, or run it from WSL/Linux against the install folder.
set -e

ROOT="${1:-$STBC_GAME_ROOT}"
MODE="${2:-}"
if [ "$1" = "undo" ]; then ROOT="$STBC_GAME_ROOT"; MODE=undo; fi
if [ -z "$ROOT" ] || [ ! -e "$ROOT/stbc.exe" ]; then
  echo "usage: bash game/install.sh \"<folder that holds stbc.exe>\" [undo]"
  echo "(or set STBC_GAME_ROOT).  stbc.exe not found in: '${ROOT}'"
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
SUF=pre-duelai.bak

"$PY" -c "
import sys
try:
    open(sys.argv[1] + '/stbc.exe', 'r+b').close()
except OSError:
    sys.exit('stbc.exe is running - close the game first')" "$ROOT"

NEW="scripts/Custom/Autoload/ClaudeCommander.py"
PATCHED="scripts/AI/PlainAI/IntelligentCircleObject.py"

backup() {   # backup <relative .py path>
  local f="$ROOT/$1"
  [ -e "$f" ] && [ ! -e "$f.$SUF" ] && cp -p "$f" "$f.$SUF"
  [ -e "${f}c" ] && [ ! -e "${f}c.$SUF" ] && cp -p "${f}c" "${f}c.$SUF"
  return 0
}

restore() {  # restore <relative .py path>
  local f="$ROOT/$1"
  if [ -e "$f.$SUF" ]; then cp -p "$f.$SUF" "$f"; echo "restored $1"
  elif [ "$1" = "$NEW" ]; then rm -f "$f"; echo "removed $1"
  fi
  if [ -e "${f}c.$SUF" ]; then cp -p "${f}c.$SUF" "${f}c"; else rm -f "${f}c"; fi
}

if [ "$MODE" = "undo" ]; then
  restore "$NEW"
  restore "$PATCHED"
  exit 0
fi

# 1. the command link (a new script, not a stock one)
mkdir -p "$ROOT/scripts/Custom/Autoload"
if cmp -s "$HERE/$NEW" "$ROOT/$NEW"; then
  echo "ClaudeCommander already installed"
else
  backup "$NEW"
  cp "$HERE/$NEW" "$ROOT/$NEW"
  rm -f "$ROOT/${NEW}c"
  echo "installed ClaudeCommander"
fi

# 2. patches to the game's own scripts
for p in "$HERE"/patches/*.patch; do
  name="$(basename "$p")"
  if (cd "$ROOT" && patch -p1 --binary -R --dry-run -s -f < "$p" >/dev/null 2>&1); then
    echo "$name already applied"; continue
  fi
  if ! (cd "$ROOT" && patch -p1 --binary --dry-run -s -f < "$p" >/dev/null 2>&1); then
    echo "$name does not apply cleanly - your copy differs from the one it was"
    echo "made against (Orion Delta 4.1).  Nothing patched; apply it by hand."
    exit 1
  fi
  for f in $(grep '^+++ ' "$p" | sed 's/^+++ b\///; s/\t.*//'); do
    backup "$f"
    rm -f "$ROOT/${f}c"
  done
  (cd "$ROOT" && patch -p1 --binary -s --no-backup-if-mismatch < "$p")
  echo "applied $name"
done

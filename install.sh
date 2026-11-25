#!/usr/bin/env bash
# =============================================================================
# sic-pr-caddie installer. Copies the agent, hooks and scripts into ~/.claude.
# =============================================================================
#     ./install.sh [--dry-run]
#     ./install.sh --uninstall [--dry-run]
#
# Install, safe to run again (for example after a git pull):
#   agents/sic-pr-caddie.md          -> ~/.claude/agents/sic-pr-caddie.md
#   hooks/sic-pr-caddie-guard.py     -> ~/.claude/hooks/sic-pr-caddie-guard.py   (used by the agent's frontmatter)
#   hooks/pr-opened.py           -> ~/.claude/hooks/pr-opened.py         (optional launcher, see README)
#   scripts/sic-pr-caddie-watch.py   -> ~/.claude/scripts/sic-pr-caddie-watch.py
#   scripts/sic-pr-caddie-gate.py    -> ~/.claude/scripts/sic-pr-caddie-gate.py
#   examples/sic-pr-caddie.json      -> ~/.claude/sic-pr-caddie.json             (only if there is none yet)
# A file that is already there and differs is backed up first as <file>.bak-<time>. Identical files are skipped.
# It never edits ~/.claude/settings.json: it prints the block for the optional launcher hook instead.
#
# Uninstall removes the five installed files. A file that differs from this repository's copy (you changed it)
# is moved to <file>.bak-<time> instead of deleted. It keeps ~/.claude/sic-pr-caddie.json and the logs in
# ~/.local/state/sic-pr-caddie/; it prints how to remove them by hand.
#
# No network. Exit codes: 0 done, 1 a check failed, 2 a usage error.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="$HOME/.claude"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILES=(
  "agents/sic-pr-caddie.md:agents/sic-pr-caddie.md"
  "hooks/sic-pr-caddie-guard.py:hooks/sic-pr-caddie-guard.py"
  "hooks/pr-opened.py:hooks/pr-opened.py"
  "scripts/sic-pr-caddie-watch.py:scripts/sic-pr-caddie-watch.py"
  "scripts/sic-pr-caddie-gate.py:scripts/sic-pr-caddie-gate.py"
)
CONFIG_SRC="examples/sic-pr-caddie.json"
CONFIG_DST="$CLAUDE_HOME/sic-pr-caddie.json"

DRY=0; UNINSTALL=0
usage() { sed -n '5,6p' "$0" | sed 's/^# \{0,5\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    -h|--help) usage;;
    *) echo "install.sh: unknown argument $1" >&2; usage;;
  esac
done

t() { printf '%s' "${1/#"$HOME"/\~}"; }
say() { if [ "$DRY" = 1 ]; then echo "would: $*"; else echo "done:  $*"; fi; }
run() { if [ "$DRY" = 0 ]; then "$@"; fi; }
fail() { echo "install.sh: $*" >&2; exit 1; }
backup_name() {
  local b="$1.bak-$STAMP" n=2
  while [ -e "$b" ]; do b="$1.bak-$STAMP-$n"; n=$((n + 1)); done
  printf '%s' "$b"
}

for pair in "${FILES[@]}"; do [ -f "$REPO/${pair%%:*}" ] || fail "run this from the sic-pr-caddie repository (missing ${pair%%:*})"; done
if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ "$(cd "$CLAUDE_CONFIG_DIR" 2>/dev/null && pwd)" != "$(cd "$CLAUDE_HOME" 2>/dev/null && pwd)" ]; then
  fail "CLAUDE_CONFIG_DIR is set to $CLAUDE_CONFIG_DIR. This installer and the agent's paths support only ~/.claude."
fi

if [ "$UNINSTALL" = 1 ]; then
  for pair in "${FILES[@]}"; do
    src="$REPO/${pair%%:*}"; dst="$CLAUDE_HOME/${pair##*:}"
    if [ ! -e "$dst" ]; then echo "skip:  $(t "$dst") is not there"; continue; fi
    if cmp -s "$src" "$dst"; then
      run rm -f "$dst"; say "removed $(t "$dst")"
    else
      b="$(backup_name "$dst")"; run mv "$dst" "$b"; say "moved $(t "$dst") to $(t "$b") (it differs from this repository's copy)"
    fi
  done
  echo
  echo "Kept: $(t "$CONFIG_DST") and the logs in ~/.local/state/sic-pr-caddie/. Remove them by hand if you want them gone:"
  echo "  rm -f ~/.claude/sic-pr-caddie.json; rm -rf ~/.local/state/sic-pr-caddie"
  echo "If you added the pr-opened hook to ~/.claude/settings.json, remove that entry too."
  exit 0
fi

command -v python3 >/dev/null 2>&1 || fail "python3 is required (3.8 or later)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || fail "python3 3.8 or later is required"
command -v gh >/dev/null 2>&1 || echo "note:  gh (GitHub CLI) is not on PATH; the agent needs it signed in (gh auth login)"
command -v jq >/dev/null 2>&1 || echo "note:  jq is not on PATH; the agent's thread query uses it"

for pair in "${FILES[@]}"; do
  src="$REPO/${pair%%:*}"; dst="$CLAUDE_HOME/${pair##*:}"
  run mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then
    if cmp -s "$src" "$dst"; then echo "same:  $(t "$dst")"; continue; fi
    b="$(backup_name "$dst")"; run cp -p "$dst" "$b"; say "backed up $(t "$dst") to $(t "$b")"
  fi
  run cp "$src" "$dst"
  case "$dst" in *.py) run chmod 755 "$dst";; *) run chmod 644 "$dst";; esac
  say "installed $(t "$dst")"
done

if [ -e "$CONFIG_DST" ]; then
  echo "kept:  $(t "$CONFIG_DST") (yours; compare with $CONFIG_SRC for new keys)"
else
  run cp "$REPO/$CONFIG_SRC" "$CONFIG_DST"; run chmod 644 "$CONFIG_DST"
  say "copied the example config to $(t "$CONFIG_DST")"
fi

cat <<'EOF'

Next:
  1. Edit ~/.claude/sic-pr-caddie.json: required checks and review bots per repo (README, "Configure").
  2. Optional launcher: to have Claude offer the caddie after every `gh pr create`, add this to the
     "hooks" object in ~/.claude/settings.json (this installer does not edit settings.json):

     "PostToolUse": [
       { "matcher": "Bash",
         "hooks": [ { "type": "command", "command": "python3 -I ~/.claude/hooks/pr-opened.py" } ] }
     ]

  3. Start a new Claude Code session so it loads the agent. Kill switch: touch ~/.claude/sic-pr-caddie.off
EOF

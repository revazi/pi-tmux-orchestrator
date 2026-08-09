#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AGENT_HOME=${PI_AGENT_HOME:-"$HOME/.pi/agent"}
SKILLS_HOME="$AGENT_HOME/skills"
BIN_HOME="$AGENT_HOME/bin"
BACKUP_HOME="$AGENT_HOME/backups"
TARGET="$SKILLS_HOME/tmux-agent-orchestrator"
TEMP_TARGET="$SKILLS_HOME/.tmux-agent-orchestrator.install.$$"

cleanup() {
  rm -rf "$TEMP_TARGET"
}
trap cleanup EXIT

mkdir -p "$SKILLS_HOME" "$BIN_HOME" "$BACKUP_HOME"
chmod 700 "$AGENT_HOME" "$SKILLS_HOME" "$BIN_HOME" "$BACKUP_HOME" 2>/dev/null || true

mkdir -p "$TEMP_TARGET/bin" "$TEMP_TARGET/pi_tmux_orchestrator" "$TEMP_TARGET/references"
cp "$ROOT/SKILL.md" "$TEMP_TARGET/SKILL.md"
cp "$ROOT/bin/pi-tmux-agents" "$TEMP_TARGET/bin/pi-tmux-agents"
cp "$ROOT/pi_tmux_orchestrator/"*.py "$TEMP_TARGET/pi_tmux_orchestrator/"
cp "$ROOT/references/usage.md" "$TEMP_TARGET/references/usage.md"
chmod 700 \
  "$TEMP_TARGET" \
  "$TEMP_TARGET/bin" \
  "$TEMP_TARGET/pi_tmux_orchestrator" \
  "$TEMP_TARGET/references"
chmod 600 \
  "$TEMP_TARGET/SKILL.md" \
  "$TEMP_TARGET/references/usage.md" \
  "$TEMP_TARGET/pi_tmux_orchestrator/"*.py
chmod 700 "$TEMP_TARGET/bin/pi-tmux-agents"

if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  if diff -qr "$TARGET" "$TEMP_TARGET" >/dev/null 2>&1; then
    rm -rf "$TARGET"
  else
    BACKUP="$BACKUP_HOME/tmux-agent-orchestrator.$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$TARGET" "$BACKUP"
    printf 'Previous installation backed up to %s\n' "$BACKUP"
  fi
fi
mv "$TEMP_TARGET" "$TARGET"
ln -sfn "$TARGET/bin/pi-tmux-agents" "$BIN_HOME/pi-tmux-agents"

printf 'Installed Pi skill: %s\n' "$TARGET"
printf 'Installed CLI:      %s\n' "$BIN_HOME/pi-tmux-agents"
if [[ ":$PATH:" != *":$BIN_HOME:"* ]]; then
  printf 'Warning: add %s to PATH.\n' "$BIN_HOME"
fi
printf 'Start a new Pi session, then use /skill:tmux-agent-orchestrator.\n'

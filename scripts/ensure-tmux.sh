#!/usr/bin/env bash
set -euo pipefail

if command -v tmux >/dev/null 2>&1; then
  tmux -V
  exit 0
fi

if ! command -v apt-get >/dev/null 2>&1 || ! command -v timeout >/dev/null 2>&1; then
  printf '%s\n' 'tmux is unavailable and this runner has no bounded apt-get installer' >&2
  exit 1
fi

APT_TIMEOUT_SECONDS=${PI_TMUX_APT_TIMEOUT_SECONDS:-120}
case "$APT_TIMEOUT_SECONDS" in
  ''|*[!0-9]*)
    printf '%s\n' 'PI_TMUX_APT_TIMEOUT_SECONDS must be a positive integer' >&2
    exit 1
    ;;
esac
if (( APT_TIMEOUT_SECONDS < 1 || APT_TIMEOUT_SECONDS > 600 )); then
  printf '%s\n' 'PI_TMUX_APT_TIMEOUT_SECONDS must be between 1 and 600' >&2
  exit 1
fi

bounded_apt() {
  sudo timeout --signal=TERM --kill-after=10s "${APT_TIMEOUT_SECONDS}s" \
    apt-get \
      -o Acquire::Retries=2 \
      -o Acquire::http::Timeout=20 \
      -o Acquire::https::Timeout=20 \
      -o DPkg::Lock::Timeout=30 \
      "$@"
}

bounded_apt update
bounded_apt install --yes --no-install-recommends tmux
command -v tmux >/dev/null 2>&1
tmux -V

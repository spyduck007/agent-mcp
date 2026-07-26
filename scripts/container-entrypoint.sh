#!/bin/sh
set -eu

mkdir -p   "${COMMAND_HOME:-/root}/.config"   "${COMMAND_HOME:-/root}/.cache"   "${COMMAND_HOME:-/root}/.local/bin"   "${TOOL_ROOT:-/opt/agent-tools}/bin"

git config --system --replace-all safe.directory '*' || true
if command -v gh >/dev/null 2>&1 && HOME="${COMMAND_HOME:-/root}" gh auth status >/dev/null 2>&1; then
  HOME="${COMMAND_HOME:-/root}" gh auth setup-git >/dev/null 2>&1 || true
fi

exec "$@"

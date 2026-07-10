#!/bin/sh
# Create an isolated, credential-free clone for the authenticated MCP agent.
# Run from the repository root on the host, not inside the MCP container.
set -eu

REPOSITORY=${SELF_IMPROVEMENT_REPOSITORY:-https://github.com/spyduck007/agent-mcp.git}
DESTINATION=${SELF_IMPROVEMENT_WORKSPACE_DIR:-./workspaces/agent-mcp}
BRANCH=${SELF_IMPROVEMENT_BASE_BRANCH:-main}

case "$DESTINATION" in
  ./workspaces/*) ;;
  *) echo "Destination must remain below ./workspaces" >&2; exit 1 ;;
esac

if [ -e "$DESTINATION" ]; then
  if [ ! -d "$DESTINATION/.git" ]; then
    echo "Refusing to use existing non-Git directory: $DESTINATION" >&2
    exit 1
  fi
  git -C "$DESTINATION" remote set-url origin "$REPOSITORY"
  git -C "$DESTINATION" fetch origin "$BRANCH" --prune
  git -C "$DESTINATION" checkout "$BRANCH"
  git -C "$DESTINATION" pull --ff-only origin "$BRANCH"
else
  mkdir -p "$(dirname "$DESTINATION")"
  git clone --branch "$BRANCH" --single-branch "$REPOSITORY" "$DESTINATION"
fi

# Identity only; this script deliberately does not configure a token or credential helper.
git -C "$DESTINATION" config user.name "Agent MCP"
git -C "$DESTINATION" config user.email "agent-mcp@localhost"
printf 'Self-improvement workspace ready at %s\n' "$DESTINATION"
printf 'It is an isolated clone. Add GITHUB_TOKEN to config/secrets.json before allowing PR creation.\n'

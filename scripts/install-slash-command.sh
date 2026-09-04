#!/usr/bin/env bash
#
# Install the /duet slash command for Claude Code, and the matching /duet prompt for the
# Codex CLI. Both are plain markdown files, so this only ever copies files into your own
# config directories. It never uses sudo and never touches anything else.
#
# Usage:
#   ./scripts/install-slash-command.sh              # install for both CLIs
#   ./scripts/install-slash-command.sh --claude     # Claude Code only
#   ./scripts/install-slash-command.sh --codex      # Codex only
#   ./scripts/install-slash-command.sh --uninstall  # remove what this installed
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$REPO_ROOT/commands/duet.md"

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}/prompts"

do_claude=true
do_codex=true
uninstall=false

for arg in "$@"; do
  case "$arg" in
    --claude)    do_codex=false ;;
    --codex)     do_claude=false ;;
    --uninstall) uninstall=true ;;
    -h|--help)   sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ ! -f "$SOURCE" ]; then
  echo "error: $SOURCE not found. Run this from a clone of the repository." >&2
  exit 1
fi

install_to() {
  local dir="$1" label="$2"
  if [ "$uninstall" = true ]; then
    if [ -f "$dir/duet.md" ]; then
      rm -f "$dir/duet.md"
      echo "removed  $dir/duet.md  ($label)"
    else
      echo "skipped  $dir/duet.md  (not present)"
    fi
    return
  fi
  mkdir -p "$dir"
  if [ -f "$dir/duet.md" ] && ! cmp -s "$SOURCE" "$dir/duet.md"; then
    cp "$dir/duet.md" "$dir/duet.md.bak"
    echo "backed up existing command to $dir/duet.md.bak"
  fi
  cp "$SOURCE" "$dir/duet.md"
  echo "installed $dir/duet.md  ($label)"
}

[ "$do_claude" = true ] && install_to "$CLAUDE_DIR" "Claude Code"
[ "$do_codex"  = true ] && install_to "$CODEX_DIR"  "Codex CLI"

if [ "$uninstall" = false ]; then
  cat <<'EOF'

Done. Type /duet in either CLI.

  /duet Add retry-with-backoff to the HTTP client and cover it with tests

The command needs the agent_duet MCP server registered and working. Check with:

  agent-duet doctor
  claude mcp get agent_duet
  codex mcp get agent_duet
EOF
fi

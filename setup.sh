#!/usr/bin/env bash
#
# agent-duet setup. One script, no hand-edited config files.
#
#   ./setup.sh [-d PATH]          guided setup; optionally register PATH directly
#   ./setup.sh install            install and register everything, no questions
#   ./setup.sh add-repo [PATH]    let agent-duet work on a project (default: here)
#   ./setup.sh remove-repo [PATH] undo that
#   ./setup.sh check              is everything working?
#   ./setup.sh demo               build a throwaway project and try it for real
#   ./setup.sh demo --clean       delete the throwaway project
#   ./setup.sh uninstall          remove the registrations and the /duet command
#
# It never uses sudo, backs up every file before changing it, and can be re-run
# safely as many times as you like.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/agent-duet"
CONFIG_FILE="$CONFIG_DIR/config.toml"
STATE_DIR="$HOME/.local/state/agent-duet"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/agent-duet"
VENV_DIR="$DATA_DIR/venv"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
CLAUDE_COMMANDS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands"
DEMO_ROOT="$HOME/duet-demo"
DEMO_REPO="$DEMO_ROOT/smoke"
DEMO_REMOTE="$DEMO_ROOT/smoke-remote.git"

ASSUME_YES=false
AUTH_PENDING=false
INSTALL_TEMP_DIR=""
PROJECT_DIR=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi

step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$D" "$*" "$N"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%serror:%s %s\n\n' "$R" "$N" "$*" >&2; exit 1; }

ask_yes() {  # ask_yes "question" -> 0 for yes
  local prompt="$1"
  if [ ! -t 0 ]; then return 1; fi
  local reply
  read -r -p "    $prompt [Y/n] " reply || true
  case "${reply:-y}" in [Nn]*) return 1 ;; *) return 0 ;; esac
}

ask_explicit_consent() {
  local reply
  if [ ! -t 0 ]; then return 1; fi
  read -r -p "    $1 [y/N] " reply || true
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

ask_consent() {  # Required installations default to no; --yes consents.
  if [ "$ASSUME_YES" = true ]; then return 0; fi
  ask_explicit_consent "$1"
}

not_completed() {
  printf '\n%sinstallation not completed.%s Nothing was changed by this step.\n\n' "$Y" "$N" >&2
  exit 2
}

cleanup_install_temp() {
  if [ -n "$INSTALL_TEMP_DIR" ] && [ -d "$INSTALL_TEMP_DIR" ]; then
    rm -rf -- "$INSTALL_TEMP_DIR"
  fi
  INSTALL_TEMP_DIR=""
}

prompt_for_repo() {
  local target
  while true; do
    read -r -p "    Project repository path (relative, absolute, or ~/...; blank to skip): " \
      target || true
    if [ -z "$target" ]; then
      info "No project registered. Later: ./setup.sh add-repo /path/to/your/project"
      return 0
    fi
    case "$target" in
      "~") target="$HOME" ;;
      "~/"*) target="$HOME/${target#\~/}" ;;
    esac
    if [ ! -d "$target" ]; then
      warn "$target does not exist."
      info "Enter an existing project folder, or leave it blank to skip."
      continue
    fi
    if do_add_repo "$target"; then
      return 0
    fi
  done
}

# ---------------------------------------------------------------- python ----
# Everything that touches a .toml file goes through Python, so a config is only
# ever written after it has been parsed back and checked.

python_is_compatible() {
  local candidate="$1"
  [ -x "$candidate" ] || return 1
  "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))' \
    >/dev/null 2>&1
}

find_conda() {
  local candidate=""
  if [ -n "${CONDA_EXE:-}" ] && [ -x "$CONDA_EXE" ]; then
    printf '%s\n' "$CONDA_EXE"
    return 0
  fi
  candidate="$(command -v conda 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  if [ -x "$HOME/miniconda3/bin/conda" ]; then
    printf '%s\n' "$HOME/miniconda3/bin/conda"
    return 0
  fi
  return 1
}

conda_agent_python() {
  local conda_bin="$1"
  "$conda_bin" run --name agent-duet python -c \
    'import sys; print(sys.executable, end="")' 2>/dev/null
}

prepare_python_environment() {
  local conda_bin env_python system_python

  conda_bin="$(find_conda || true)"
  if [ -n "$conda_bin" ]; then
    env_python="$(conda_agent_python "$conda_bin" || true)"
    if [ -n "$env_python" ] && python_is_compatible "$env_python"; then
      DUET_PYTHON="$env_python"
      export DUET_PYTHON
      ok "using existing dedicated Conda environment: $env_python"
      return 0
    fi

    step "Preparing an isolated Python environment"
    info "Conda was detected. Agent Duet will use a dedicated Conda environment named agent-duet."
    info "The base environment and every other environment will be left unchanged."
    if ! ask_consent "Create the dedicated Conda environment now?"; then not_completed; fi
    if [ -n "$env_python" ]; then
      "$conda_bin" install --name agent-duet --yes python=3.13 pip \
        || die "could not repair the agent-duet Conda environment."
    else
      "$conda_bin" create --name agent-duet --yes python=3.13 pip \
        || die "could not create the agent-duet Conda environment."
    fi
    env_python="$(conda_agent_python "$conda_bin" || true)"
    [ -n "$env_python" ] \
      || die "Conda finished, but the agent-duet environment could not be located."
    python_is_compatible "$env_python" \
      || die "Conda finished, but $env_python is not Python 3.13 or newer."
    DUET_PYTHON="$env_python"
    export DUET_PYTHON
    ok "dedicated environment ready: $env_python"
    return 0
  fi

  env_python="$VENV_DIR/bin/python"
  if python_is_compatible "$env_python"; then
    DUET_PYTHON="$env_python"
    export DUET_PYTHON
    ok "using existing private Python environment: $env_python"
    return 0
  fi

  system_python="$(command -v python3 2>/dev/null || true)"
  [ -n "$system_python" ] \
    || die "python3 was not found. Install Python 3.13 or newer, then run ./setup.sh again."
  python_is_compatible "$system_python" \
    || die "$system_python is too old. Install Python 3.13 or newer, then run ./setup.sh again."

  step "Preparing an isolated Python environment"
  info "Conda was not found. Agent Duet will create a private Python environment at:"
  info "  $VENV_DIR"
  info "System Python packages will be left unchanged."
  if ! ask_consent "Create the private Python environment now?"; then not_completed; fi
  mkdir -p "$DATA_DIR"
  "$system_python" -m venv "$VENV_DIR" \
    || die "Python could not create a virtual environment. Install its venv support and retry."
  python_is_compatible "$env_python" \
    || die "the private environment was created without Python 3.13 or newer."
  DUET_PYTHON="$env_python"
  export DUET_PYTHON
  ok "private environment ready: $env_python"
}

pick_python() {
  local bin conda_bin
  conda_bin="$(find_conda || true)"
  if [ -n "$conda_bin" ]; then
    bin="$(conda_agent_python "$conda_bin" || true)"
    if [ -n "$bin" ] && python_is_compatible "$bin"; then echo "$bin"; return; fi
    die "the dedicated Python environment is missing; run ./setup.sh first."
  fi
  if python_is_compatible "$VENV_DIR/bin/python"; then
    echo "$VENV_DIR/bin/python"
    return
  fi
  die "the private Python environment is missing; run ./setup.sh first."
}

require_python_version() {
  "$PY" - <<'PY' || die "$PY is too old. agent-duet needs Python 3.13 or newer."
import sys
raise SystemExit(0 if sys.version_info >= (3, 13) else 1)
PY
}

# ---------------------------------------------------------- provider CLIs ----

refresh_user_path() {
  local directory
  for directory in "${CODEX_INSTALL_DIR:-}" "$HOME/.local/bin" \
                   "$HOME/.claude/local/bin" "$HOME/.codex/bin"; do
    [ -n "$directory" ] || continue
    case ":$PATH:" in
      *":$directory:"*) ;;
      *) PATH="$directory:$PATH" ;;
    esac
  done
  export PATH
  hash -r
}

preflight_guided_setup() {
  refresh_user_path
  [ "$(uname -s)" = "Linux" ] || die "the guided installer currently supports Linux only."
  command -v git >/dev/null 2>&1 || die "git was not found. Install Git, then run ./setup.sh again."
  if [ -n "${CODEX_INSTALL_DIR:-}" ]; then
    case "$CODEX_INSTALL_DIR" in
      /*) ;;
      *) die "CODEX_INSTALL_DIR must be an absolute path." ;;
    esac
  fi
  if { ! command -v claude >/dev/null 2>&1 || ! command -v codex >/dev/null 2>&1; } &&
     ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    die "install curl or wget, then run ./setup.sh again."
  fi
}

download_installer() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$destination" "$url"
  else
    die "install curl or wget, then run ./setup.sh again."
  fi
}

install_provider_cli() {
  local label="$1" command_name="$2" url="$3" expected_changes="$4"
  refresh_user_path
  if command -v "$command_name" >/dev/null 2>&1; then return 0; fi

  step "Installing $label"
  info "$label is not installed. The official installer is:"
  info "  $url"
  info "$expected_changes"
  info "The installer may maintain its own updates. setup.sh never uses sudo."
  if ! ask_consent "Download and run this official installer now?"; then not_completed; fi

  INSTALL_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-duet-install.XXXXXX")" \
    || die "could not create a temporary installer directory."
  trap cleanup_install_temp EXIT
  if ! download_installer "$url" "$INSTALL_TEMP_DIR/install.sh"; then
    die "could not download $url"
  fi
  if ! bash "$INSTALL_TEMP_DIR/install.sh"; then
    die "$label's official installer failed."
  fi
  cleanup_install_temp
  trap - EXIT
  refresh_user_path
  command -v "$command_name" >/dev/null 2>&1 \
    || die "$label installed, but '$command_name' is still not on PATH. Open a new terminal and retry."
  ok "$label installed"
}

prepare_provider_clis() {
  local codex_bin_dir="${CODEX_INSTALL_DIR:-$HOME/.local/bin}"
  local codex_home="${CODEX_HOME:-$HOME/.codex}"
  local shell_profile="$HOME/.profile"
  local claude_changes codex_changes
  case "${SHELL##*/}" in
    bash) shell_profile="$HOME/.bashrc" ;;
    zsh) shell_profile="$HOME/.zshrc" ;;
  esac
  claude_changes="Expected user files: $HOME/.local/bin/claude and files under $HOME/.claude; its installer may update shell integration."
  codex_changes="Expected user files: $codex_bin_dir/codex and $codex_home/packages/standalone; when needed, its installer adds a managed PATH block to $shell_profile."
  install_provider_cli \
    "Claude Code" "claude" "https://claude.ai/install.sh" \
    "$claude_changes"
  install_provider_cli \
    "Codex" "codex" "https://chatgpt.com/codex/install.sh" \
    "$codex_changes"
}

offer_provider_logins() {
  local codex_login="codex login"

  if claude auth status >/dev/null 2>&1; then
    ok "Claude Code is signed in"
  else
    warn "Claude Code is not signed in."
    if [ ! -t 0 ]; then
      info "Later, run: claude auth login"
      AUTH_PENDING=true
    elif ask_explicit_consent "Start Claude Code sign-in now?"; then
      claude auth login || die "Claude Code sign-in did not finish successfully."
      ok "Claude Code sign-in finished"
    else
      info "Later, run: claude auth login"
      AUTH_PENDING=true
    fi
  fi

  if [ ! -t 0 ] || [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ] || \
     { [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; }; then
    codex_login="codex login --device-auth"
  fi
  if codex login status >/dev/null 2>&1; then
    ok "Codex is signed in"
  else
    warn "Codex is not signed in."
    if [ ! -t 0 ]; then
      info "Later, run: $codex_login"
      AUTH_PENDING=true
    elif ask_explicit_consent "Start Codex sign-in now?"; then
      if [ "$codex_login" = "codex login --device-auth" ]; then
        codex login --device-auth || die "Codex sign-in did not finish successfully."
      else
        codex login || die "Codex sign-in did not finish successfully."
      fi
      ok "Codex sign-in finished"
    else
      info "Later, run: $codex_login"
      AUTH_PENDING=true
    fi
  fi
}

install_agent_launcher() {
  local duet_bin="$1" launcher="$HOME/.local/bin/agent-duet"
  if [ "$duet_bin" = "$launcher" ]; then return 0; fi
  mkdir -p "$(dirname "$launcher")"
  if [ -L "$launcher" ] && [ "$(readlink "$launcher")" = "$duet_bin" ]; then
    return 0
  fi
  [ ! -d "$launcher" ] || die "$launcher is a directory; move it aside and retry."
  if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    cp -P "$launcher" "$launcher.duet-backup" \
      || die "could not back up the existing $launcher."
    note "backed up your old agent-duet launcher"
  fi
  ln -sfn "$duet_bin" "$launcher" || die "could not create $launcher."
  refresh_user_path
  ok "command launcher  $launcher"
}

# --------------------------------------------------------------- install ----

do_install() {
  step "Checking what is already on this machine"

  local claude_bin codex_bin
  claude_bin="$(command -v claude 2>/dev/null || true)"
  codex_bin="$(command -v codex 2>/dev/null || true)"
  [ -n "$claude_bin" ] || die "the 'claude' command is not installed or not on your PATH."
  [ -n "$codex_bin" ]  || die "the 'codex' command is not installed or not on your PATH."
  ok "claude   $claude_bin  ($(claude --version 2>&1 | head -1))"
  ok "codex    $codex_bin  ($(codex --version 2>&1 | head -1))"

  PY="$(pick_python)"
  require_python_version
  ok "python   $PY  ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

  step "Installing agent-duet"
  if [ -f "$REPO_ROOT/pyproject.toml" ]; then
    [ -f "$REPO_ROOT/requirements-lock.txt" ] \
      || die "$REPO_ROOT/requirements-lock.txt is missing."
    info "Installing locked dependencies (the first run can take a few minutes)..."
    "$PY" -m pip install --disable-pip-version-check \
      -r "$REPO_ROOT/requirements-lock.txt" \
      || die "locked dependency installation failed. Scroll up for the reason."
    info "Installing Agent Duet itself..."
    "$PY" -m pip install --disable-pip-version-check --no-deps \
      --editable "$REPO_ROOT" \
      || die "pip install failed. Scroll up for the reason."
    ok "installed from $REPO_ROOT"
  else
    command -v agent-duet >/dev/null 2>&1 \
      || die "run this script from inside the cloned repository."
    ok "already installed"
  fi

  local duet_bin
  duet_bin="$(dirname "$PY")/agent-duet"
  if [ ! -x "$duet_bin" ]; then
    duet_bin="$("$PY" -c 'import shutil; p=shutil.which("agent-duet"); print(p or "", end="")')"
  fi
  [ -x "$duet_bin" ] || die "agent-duet installed but $duet_bin is not executable."
  ok "agent-duet  $duet_bin"
  install_agent_launcher "$duet_bin"

  step "Writing your configuration"
  mkdir -p "$CONFIG_DIR" "$STATE_DIR"
  chmod 700 "$CONFIG_DIR" "$STATE_DIR"
  if [ -f "$CONFIG_FILE" ]; then
    ok "keeping the config you already have: $CONFIG_FILE"
  else
    "$PY" - "$REPO_ROOT/config.example.toml" "$CONFIG_FILE" "$HOME" "$STATE_DIR" \
             "$claude_bin" "$codex_bin" <<'PY'
import pathlib, re, sys, tomllib

example, out, home, state_dir, claude_bin, codex_bin = sys.argv[1:7]
text = pathlib.Path(example).read_text()

# Replace the "copy this and edit it by hand" header; that already happened.
text = text.split("\n\n", 1)[1]
text = ("# agent-duet configuration, written by setup.sh.\n"
        "#\n"
        "# Edit it freely. Unknown keys are a hard error, so a typo fails loudly\n"
        "# instead of being ignored. Run `agent-duet doctor` after any change.\n"
        "#\n"
        "# To let agent-duet work on a project, prefer:  ./setup.sh add-repo /path/to/it\n\n") + text

# The example ships with REPLACE_ME placeholders. Fill in the real machine.
default_root = str(pathlib.Path(home) / "code")
text = re.sub(r'^allowed_repo_roots\s*=.*$',
              f'allowed_repo_roots = ["{default_root}"]', text, count=1, flags=re.M)
text = re.sub(r'^state_dir\s*=.*$',
              f'state_dir = "{state_dir}"', text, count=1, flags=re.M)


def set_in_table(body: str, table: str, key: str, value: str) -> str:
    """Set key inside [table] only, so two identically named keys cannot collide."""
    head = re.search(rf"^\[{re.escape(table)}\]\s*$", body, flags=re.M)
    if not head:
        sys.exit(f"config.example.toml has no [{table}] table")
    rest = body[head.end():]
    nxt = re.search(r"^\[", rest, flags=re.M)
    end = head.end() + (nxt.start() if nxt else len(rest))
    section, count = re.subn(rf"^{re.escape(key)}\s*=.*$", f'{key} = "{value}"',
                             body[head.end():end], count=1, flags=re.M)
    if count != 1:
        sys.exit(f"config.example.toml has no {key} in [{table}]")
    return body[:head.end()] + section + body[end:]


text = set_in_table(text, "claude", "executable", claude_bin)
text = set_in_table(text, "codex", "executable", codex_bin)

# The commented-out examples further down mention paths too; make them read
# naturally for this machine rather than leaving REPLACE_ME lying around.
text = text.replace("/home/REPLACE_ME", home)
if "REPLACE_ME" in text:
    sys.exit("config.example.toml has a placeholder this script does not know how to fill")

data = tomllib.loads(text)                      # refuse to write anything unparseable
assert data["claude"]["executable"] == claude_bin
assert data["codex"]["executable"] == codex_bin

path = pathlib.Path(out)
path.write_text(text)
path.chmod(0o600)
print(f"    created {path}")
print(f"    projects may live anywhere under {default_root}")
PY
    ok "config written and locked to your account only (mode 600)"
  fi

  step "Registering agent-duet with Claude Code"
  claude mcp remove agent_duet --scope user >/dev/null 2>&1 || true
  claude mcp add-json --scope user agent_duet \
    "{\"type\":\"stdio\",\"command\":\"$duet_bin\",\"args\":[],\"env\":{},\"timeout\":120000}" \
    >/dev/null || die "claude mcp add-json failed."
  ok "registered (120s tool timeout; duet_wait returns within 90s)"

  step "Registering agent-duet with Codex"
  codex mcp remove agent_duet >/dev/null 2>&1 || true
  codex mcp add agent_duet -- "$duet_bin" >/dev/null || die "codex mcp add failed."
  # codex mcp add cannot set timeouts or the tool allowlist, so finish the table here.
  "$PY" - "$CODEX_HOME_DIR/config.toml" "$duet_bin" <<'PY'
import pathlib, re, shutil, sys, tomllib

target, duet_bin = pathlib.Path(sys.argv[1]), sys.argv[2]
desired = f'''[mcp_servers.agent_duet]
command = "{duet_bin}"
args = []
startup_timeout_sec = 20
# duet_wait is hard-capped at 90s; this leaves transport overhead without a long hang.
tool_timeout_sec = 120
enabled_tools = ["duet_start", "duet_status", "duet_wait", "duet_cancel", "duet_finalize"]
'''

original = target.read_text() if target.exists() else ""
lines = original.splitlines(keepends=True)

start = next((i for i, ln in enumerate(lines)
              if ln.strip() == "[mcp_servers.agent_duet]"), None)
if start is None:
    body = original if original.endswith("\n") or not original else original + "\n"
    updated = (body + "\n" + desired) if body else desired
else:
    # Stop at the next top-level table, but keep walking over our own subtables
    # ([mcp_servers.agent_duet.env] and friends belong to us, not to the next one).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith("[") and not stripped.startswith("[mcp_servers.agent_duet."):
            end = i
            break
    updated = "".join(lines[:start]) + desired + "".join(lines[end:])

parsed = tomllib.loads(updated)                 # never write a broken codex config
entry = parsed["mcp_servers"]["agent_duet"]
assert entry["command"] == duet_bin, entry
assert entry["tool_timeout_sec"] == 120, entry
assert set(entry["enabled_tools"]) == {
    "duet_start", "duet_status", "duet_wait", "duet_cancel", "duet_finalize"
}, entry

if original and original != updated:
    shutil.copy2(target, target.with_suffix(".toml.duet-backup"))
    print(f"    backed up your old config to {target.with_suffix('.toml.duet-backup')}")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(updated)
PY
  ok "registered, with the five duet tools enabled"

  step "Installing the /duet command"
  install_command_file "$CLAUDE_COMMANDS_DIR" "Claude Code"
  install_command_file "$CODEX_HOME_DIR/prompts" "Codex"
}

print_setup_complete() {
  printf '\n%sSetup is done.%s\n' "$G$B" "$N"
  if [ -n "$PROJECT_DIR" ]; then
    info "Next: registering the project supplied with --directory"
  else
    info "Next:  ./setup.sh add-repo /path/to/your/project"
    info "   or: ./setup.sh demo        (try it on a throwaway project first)"
  fi
}

install_command_file() {
  local dir="$1" label="$2" src="$REPO_ROOT/commands/duet.md"
  [ -f "$src" ] || die "$src is missing. Run this from a clone of the repository."
  mkdir -p "$dir"
  if [ -f "$dir/duet.md" ] && ! cmp -s "$src" "$dir/duet.md"; then
    cp "$dir/duet.md" "$dir/duet.md.duet-backup"
    note "backed up your old duet.md"
  fi
  cp "$src" "$dir/duet.md"
  ok "$label   type /duet in a session"
}

# -------------------------------------------------------------- add-repo ----

ensure_git_baseline() {
  local target="$1" created_git=false
  if git -C "$target" rev-parse --verify HEAD >/dev/null 2>&1; then
    return 0
  fi

  warn "$target needs a local Git baseline so Claude and Codex can compare their work."
  info "With your consent, setup will initialize Git and make one local baseline commit."
  info "Every existing file not excluded by .gitignore will enter local Git history,"
  info "including sensitive files. Review .gitignore first if that may be a concern."
  info "Nothing will be uploaded, and no remote will be added."
  if ! ask_consent "Create the local Git baseline now?"; then
    info "Project was not registered. The folder was left unchanged."
    return 2
  fi

  if ! git -C "$target" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$target" init -q --initial-branch=main \
      || die "could not initialize a local Git repository in $target"
    created_git=true
  fi
  if ! git -C "$target" add --all; then
    [ "$created_git" = false ] || rm -rf -- "$target/.git"
    die "could not stage the initial project snapshot; the new Git metadata was removed"
  fi
  if ! git -C "$target" \
      -c user.name="Agent Duet Setup" \
      -c user.email="agent-duet@localhost" \
      -c commit.gpgSign=false \
      commit --no-verify --allow-empty -qm "Initialize project for Agent Duet"; then
    if [ "$created_git" = true ]; then
      rm -rf -- "$target/.git"
      die "could not create the baseline commit; the new Git metadata was removed"
    fi
    die "could not create the baseline commit in the existing Git repository"
  fi
  ok "local baseline commit created on $(git -C "$target" branch --show-current)"
}

do_add_repo() {
  local target="${1:-$PWD}"
  case "$target" in
    "~") target="$HOME" ;;
    "~/"*) target="$HOME/${target#\~/}" ;;
  esac
  [ -d "$target" ] || die "no such directory: $target"
  target="$(cd "$target" && pwd -P)"
  [ -f "$CONFIG_FILE" ] || die "run ./setup.sh first — there is no config yet."
  ensure_git_baseline "$target" || return $?
  PY="$(pick_python)"

  step "Registering $target"
  "$PY" - add "$CONFIG_FILE" "$target" <<'PY'
import json, pathlib, re, shutil, sys, tomllib

action, config_path, repo = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
repo_path = pathlib.Path(repo)
text = config_path.read_text()
data = tomllib.loads(text)

BEGIN, END = f"# >>> added by setup.sh: {repo}", f"# <<< added by setup.sh: {repo}"


def strip_block(body: str) -> str:
    return re.sub(rf"\n*{re.escape(BEGIN)}\n.*?{re.escape(END)}\n", "\n",
                  body, flags=re.S)


if action == "remove":
    if BEGIN not in text:
        print(f"    {repo} was not registered by this script; nothing to undo")
        raise SystemExit(0)
    updated = strip_block(text)
    tomllib.loads(updated)
    shutil.copy2(config_path, config_path.with_suffix(".toml.duet-backup"))
    config_path.write_text(updated)
    config_path.chmod(0o600)
    print(f"    removed {repo} from your config")
    raise SystemExit(0)

# A repository must sit strictly below an allowed root, so make sure one covers it.
roots = [pathlib.Path(r) for r in data["allowed_repo_roots"]]
if not any(r in repo_path.parents for r in roots):
    parent = repo_path.parent
    if parent == pathlib.Path.home() or parent == pathlib.Path("/"):
        raise SystemExit(
            f"    {repo} sits directly in your home directory.\n"
            "    agent-duet refuses to treat all of $HOME as workable space, so move the\n"
            f"    project one level down, e.g. {pathlib.Path.home()}/code/{repo_path.name}, "
            "and run this again."
        )
    line = re.search(r"^allowed_repo_roots\s*=\s*\[[^\]]*\]\s*$", text, flags=re.M)
    if not line:
        raise SystemExit(
            "    could not find allowed_repo_roots on a single line in your config.\n"
            f"    Add {parent} to it by hand, then run this again."
        )
    merged = json.dumps([str(r) for r in roots] + [str(parent)])
    text = text[: line.start()] + f"allowed_repo_roots = {merged}" + text[line.end():]
    print(f"    allowed projects under {parent}")

# Pick the check the coordinator will run itself once both agents are finished.
def detect(root: pathlib.Path) -> tuple[list[str], str] | None:
    py = sys.executable
    if list(root.glob("test_*.py")) or list(root.glob("*/test_*.py")) or \
       list(root.glob("tests/**/test_*.py")):
        return [py, "-m", "pytest", "-q"], "pytest"
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            if "test" in json.loads(pkg.read_text()).get("scripts", {}):
                return ["npm", "test", "--silent"], "npm test"
        except (ValueError, OSError):
            pass
    if (root / "Cargo.toml").is_file():
        return ["cargo", "test"], "cargo test"
    if (root / "go.mod").is_file():
        return ["go", "test", "./..."], "go test"
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh and any(root.rglob("*.Tests.ps1")):
        return [pwsh, "-NoProfile", "-Command", "Invoke-Pester -CI"], "Pester"
    if pwsh and (any(root.rglob("*.ps1")) or any(root.rglob("*.psm1"))):
        # No test framework, but a syntax error is still worth catching, and a repo
        # with no check at all gets nothing standing behind its runs.
        return [
            pwsh, "-NoProfile", "-Command",
            "$bad = 0; Get-ChildItem -Recurse -Include *.ps1,*.psm1,*.psd1 | "
            "ForEach-Object { $errors = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$_.FullName, [ref]$null, [ref]$errors) > $null; "
            "if ($errors) { $bad++; Write-Host $_.FullName } }; exit $bad",
        ], "PowerShell parse check (no test framework found)"
    makefile = root / "Makefile"
    if makefile.is_file() and re.search(r"^test\s*:", makefile.read_text(errors="replace"),
                                        flags=re.M):
        return ["make", "test"], "make test"
    return None

found = detect(repo_path)
if found:
    command, label = found
    rendered = "[\n  " + json.dumps(command) + ",\n]"
    print(f"    validation check: {label}")
else:
    command, rendered = [], "[]"
    print("")
    print("    WARNING: no test suite or check was found for this project.")
    print("    Nothing independent will verify a run's work -- the only thing behind")
    print("    it will be what the two models claim about their own output.")
    print("")
    print(f"    To fix that, open {config_path}, find this project's")
    print("    [[repositories]] block, and put your real check in validation_commands:")
    print("")
    print('      validation_commands = [')
    print('        ["/path/to/your/test/command", "--flag"],')
    print('      ]')
    print("")
    print("    It is a command vector, not a shell string, and it runs from the")
    print("    project root. Then re-run: agent-duet doctor")
    print("")

existing = {r["path"] for r in data.get("repositories", [])}
if repo in existing and BEGIN not in text:
    print(f"    {repo} is already in your config (added by hand); leaving it alone")
    raise SystemExit(0)

text = strip_block(text).rstrip("\n") + "\n"
text += f"""
{BEGIN}
[[repositories]]
path = "{repo}"
validation_commands = {rendered}
validation_timeout_seconds = 1800
{END}
"""

check = tomllib.loads(text)                     # parse before writing, always
assert any(r["path"] == repo for r in check["repositories"]), "entry did not take"

shutil.copy2(config_path, config_path.with_suffix(".toml.duet-backup"))
config_path.write_text(text)
config_path.chmod(0o600)
print(f"    written to {config_path}")
PY

  if agent-duet doctor >/dev/null 2>&1; then
    ok "config is valid"
    if [ "${QUIET_READY:-false}" = false ]; then
      printf '\n%sReady.%s  cd %s && claude, then type: /duet <what you want built>\n\n' \
        "$G$B" "$N" "$target"
    fi
  else
    warn "config saved, but 'agent-duet doctor' is unhappy. Run it to see why:"
    info "  agent-duet doctor"
  fi
}

do_remove_repo() {
  local target="${1:-$PWD}"
  target="$(cd "$target" 2>/dev/null && pwd -P || echo "$target")"
  [ -f "$CONFIG_FILE" ] || die "there is no config to edit."
  PY="$(pick_python)"
  step "Unregistering $target"
  "$PY" - remove "$CONFIG_FILE" "$target" <<'PY'
import pathlib, re, shutil, sys, tomllib
_, config_path, repo = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
BEGIN, END = f"# >>> added by setup.sh: {repo}", f"# <<< added by setup.sh: {repo}"
text = config_path.read_text()
if BEGIN not in text:
    print(f"    {repo} was not registered by this script; nothing to undo")
    raise SystemExit(0)
updated = re.sub(rf"\n*{re.escape(BEGIN)}\n.*?{re.escape(END)}\n", "\n", text, flags=re.S)
tomllib.loads(updated)
shutil.copy2(config_path, config_path.with_suffix(".toml.duet-backup"))
config_path.write_text(updated)
config_path.chmod(0o600)
print(f"    removed {repo} from your config")
PY
}

# ----------------------------------------------------------------- check ----

codex_registration_is_valid() {
  codex mcp get agent_duet >/dev/null 2>&1 || return 1
  "$PY" - "$CODEX_HOME_DIR/config.toml" <<'PY'
import os
import pathlib
import sys
import tomllib

required_tools = {
    "duet_start",
    "duet_status",
    "duet_wait",
    "duet_cancel",
    "duet_finalize",
}

try:
    config = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
    entry = config["mcp_servers"]["agent_duet"]
    command = entry["command"]
    enabled_tools = entry["enabled_tools"]
    tool_timeout = entry["tool_timeout_sec"]
    valid = (
        isinstance(command, str)
        and pathlib.Path(command).is_absolute()
        and pathlib.Path(command).is_file()
        and os.access(command, os.X_OK)
        and set(enabled_tools) == required_tools
        and tool_timeout >= 120
    )
except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
    valid = False

raise SystemExit(0 if valid else 1)
PY
}

do_check() {
  local failed=0
  step "Is agent-duet healthy?"
  local report
  if report="$(agent-duet doctor 2>&1)"; then
    ok "healthy"
    note "$(printf '%s' "$report" | grep -c '^  repo ') project(s) registered"
  else
    warn "doctor reported a problem:"
    printf '%s\n' "$report" | sed 's/^/      /'
    failed=1
  fi

  step "Can Claude Code see it?"
  local claude_registration
  if claude_registration="$(claude mcp get agent_duet 2>&1)" \
      && grep -q "Connected" <<<"$claude_registration"; then
    ok "connected"
  else
    warn "not connected — run ./setup.sh"; failed=1
  fi

  step "Can Codex see it?"
  PY="$(pick_python)"
  if codex_registration_is_valid; then
    ok "connected, all five tools enabled"
  else
    warn "not registered properly — run ./setup.sh"; failed=1
  fi

  step "Is the /duet command installed?"
  for f in "$CLAUDE_COMMANDS_DIR/duet.md" "$CODEX_HOME_DIR/prompts/duet.md"; do
    if [ -f "$f" ]; then ok "$f"; else warn "missing: $f"; failed=1; fi
  done

  if [ "$failed" -eq 0 ]; then
    printf '\n%sEverything works.%s\n\n' "$G$B" "$N"
  else
    printf '\n%sSomething is off. Run ./setup.sh to repair it.%s\n\n' "$Y" "$N"
    return 1
  fi
}

# ------------------------------------------------------------------ demo ----

do_demo() {
  if [ "${1:-}" = "--clean" ]; then
    step "Deleting the demo"
    [ -d "$DEMO_REPO" ] && do_remove_repo "$DEMO_REPO" || true
    if [ -f "$CONFIG_FILE" ]; then
      # Also drop the allowed root the demo added, so nothing is left behind.
      PY="$(pick_python)"
      "$PY" - "$CONFIG_FILE" "$DEMO_ROOT" <<'PY'
import json, pathlib, re, shutil, sys, tomllib
config_path, root = pathlib.Path(sys.argv[1]), sys.argv[2]
text = config_path.read_text()
data = tomllib.loads(text)
if root not in data["allowed_repo_roots"]:
    raise SystemExit(0)
keep = [r for r in data["allowed_repo_roots"] if r != root]
if not keep:                       # the config requires at least one root
    raise SystemExit(0)
line = re.search(r"^allowed_repo_roots\s*=\s*\[[^\]]*\]\s*$", text, flags=re.M)
if not line:
    raise SystemExit(0)
text = text[: line.start()] + f"allowed_repo_roots = {json.dumps(keep)}" + text[line.end():]
tomllib.loads(text)
shutil.copy2(config_path, config_path.with_suffix(".toml.duet-backup"))
config_path.write_text(text)
config_path.chmod(0o600)
print(f"    stopped allowing projects under {root}")
PY
    fi
    rm -rf "$DEMO_ROOT"
    ok "removed $DEMO_ROOT"
    return
  fi

  step "Building a throwaway project at $DEMO_REPO"
  if [ -e "$DEMO_ROOT" ]; then
    ask_yes "$DEMO_ROOT already exists. Delete and rebuild it?" \
      || die "leaving it alone. Use ./setup.sh demo --clean to remove it."
    rm -rf "$DEMO_ROOT"
  fi
  mkdir -p "$DEMO_REPO"
  git -C "$DEMO_REPO" init -q -b main .
  git -C "$DEMO_REPO" config user.email "$(git config --get user.email || echo you@example.com)"
  git -C "$DEMO_REPO" config user.name  "$(git config --get user.name  || echo duet-demo)"
  printf '__pycache__/\n*.pyc\n' > "$DEMO_REPO/.gitignore"
  cat > "$DEMO_REPO/calc.py" <<'DEMO'
#!/usr/bin/env python3
"""A tiny module."""


def multiply(a, b):
    """Return a * b."""
    return a * b
DEMO
  cat > "$DEMO_REPO/test_calc.py" <<'DEMO'
#!/usr/bin/env python3
"""Tests."""

from calc import multiply


def test_multiply():
    assert multiply(3, 4) == 12
DEMO
  git -C "$DEMO_REPO" add -A
  git -C "$DEMO_REPO" commit -q -m "initial"
  git init -q --bare -b main "$DEMO_REMOTE"
  git -C "$DEMO_REPO" remote add origin "$DEMO_REMOTE"
  git -C "$DEMO_REPO" push -q origin main
  ok "created, committed, and pushed to a local remote"

  QUIET_READY=true do_add_repo "$DEMO_REPO"

  cat <<EOF

${B}Now try it.${N} Copy these two lines:

    cd $DEMO_REPO && claude

Then inside that session, paste:

    /duet Add a pure function add(a, b) to calc.py, with tests covering integers and floats

${D}It takes 5-10 minutes. Claude writes the code, Codex reviews it, Claude answers
the review, then the tests run. It stops and shows you the evidence before it
commits anything — it will not commit on its own.${N}

When you are finished:  ./setup.sh demo --clean

EOF
}

# ------------------------------------------------------------- uninstall ----

do_uninstall() {
  step "Removing registrations"
  claude mcp remove agent_duet --scope user >/dev/null 2>&1 && ok "unregistered from Claude Code" || note "was not registered with Claude Code"
  codex mcp remove agent_duet >/dev/null 2>&1 && ok "unregistered from Codex" || note "was not registered with Codex"
  for f in "$CLAUDE_COMMANDS_DIR/duet.md" "$CODEX_HOME_DIR/prompts/duet.md"; do
    [ -f "$f" ] && rm -f "$f" && ok "removed $f" || true
  done
  note "your config at $CONFIG_FILE and past runs in $STATE_DIR were left alone"
  note "delete them yourself if you want them gone"
}

# ------------------------------------------------------------------ main ----

main() {
  local -a args=()
  local arg
  while [ "$#" -gt 0 ]; do
    arg="$1"
    case "$arg" in
      -y|--yes)
        ASSUME_YES=true
        shift
        ;;
      -d|--directory)
        [ "$#" -ge 2 ] || die "$arg requires a project directory"
        [ -z "$PROJECT_DIR" ] || die "project directory was supplied more than once"
        PROJECT_DIR="$2"
        shift 2
        ;;
      --directory=*)
        [ -z "$PROJECT_DIR" ] || die "project directory was supplied more than once"
        PROJECT_DIR="${arg#*=}"
        [ -n "$PROJECT_DIR" ] || die "--directory requires a project directory"
        shift
        ;;
      -h|--help)
        awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
          "${BASH_SOURCE[0]}"
        return 0
        ;;
      *)
        args+=("$arg")
        shift
        ;;
    esac
  done
  set -- ${args[@]+"${args[@]}"}
  refresh_user_path

  case "${1:-}" in
    install)
      do_install
      do_check
      print_setup_complete
      [ -z "$PROJECT_DIR" ] || do_add_repo "$PROJECT_DIR"
      ;;
    add-repo)
      [ -z "$PROJECT_DIR" ] \
        || die "use either add-repo PATH or --directory PATH, not both"
      do_add_repo "${2:-$PWD}"
      ;;
    remove-repo) do_remove_repo "${2:-$PWD}" ;;
    check)       do_check ;;
    demo)        do_demo "${2:-}" ;;
    uninstall)   do_uninstall ;;
    "")
      preflight_guided_setup
      prepare_python_environment
      prepare_provider_clis
      offer_provider_logins
      do_install
      do_check
      print_setup_complete
      if [ "$AUTH_PENDING" = true ]; then
        warn "Installation finished, but sign-in is still required before Agent Duet can run."
      fi
      if [ -n "$PROJECT_DIR" ]; then
        do_add_repo "$PROJECT_DIR" || true
      elif ask_yes "Try it now on a throwaway project?"; then
        do_demo
      else
        prompt_for_repo
      fi
      ;;
    *) die "unknown command: $1  (run ./setup.sh --help)" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

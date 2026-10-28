#!/bin/bash
# Install Workboard for the current user.
#
#   ./install.sh                         board host: config, CLI, empty board, agent skill + /dashvis,
#                                        and (macOS) launchd jobs for the web server and 10-minute sync
#   ./install.sh --demo                  same, with a fictional demo board to explore
#   ./install.sh --remote HOST --board-dir PATH [--machine ID]
#                                        another machine: `workboard` forwards over SSH to HOST
#   ./install.sh --uninstall             remove links, launchd jobs, the hook and the skills/commands it installed
#
# The herdr kit (orchestration, lead and handover skills, oc-run, start-opencode-lead, herdr-link,
# lead-* tripwire commands) is installed too; leave it out with --no-herdr.
#
# Options: --owner ID (default: $USER)  --owner-name NAME  --no-launchd  --no-agents  --no-herdr
#          --handover-hook (add the lead-canary UserPromptSubmit hook to ~/.claude/settings.json)  --force
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/workboard"
CONFIG="$CONFIG_DIR/config.env"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
JOBS=(dev.workboard.web dev.workboard.sync)

demo=0 remote_host="" board_dir="" machine="" owner="" owner_name="" launchd=1 agents=1 herdr=1 hook=0 force=0 uninstall=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo) demo=1 ;;
    --remote) remote_host="${2:?--remote needs an ssh host}"; shift ;;
    --board-dir) board_dir="${2:?--board-dir needs a path}"; shift ;;
    --machine) machine="${2:?--machine needs an id}"; shift ;;
    --owner) owner="${2:?--owner needs an id}"; shift ;;
    --owner-name) owner_name="${2:?--owner-name needs a name}"; shift ;;
    --no-launchd) launchd=0 ;;
    --no-agents) agents=0 ;;
    --no-herdr) herdr=0 ;;
    --handover-hook) hook=1 ;;
    --force) force=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '  %s\n' "$*"; }

# Agent homes: skill dir and slash-command dir per tool. Only tools that are installed get files.
agent_targets() {
  [[ -d "$HOME/.claude" ]] && echo "$HOME/.claude/skills|$HOME/.claude/commands"
  [[ -d "$HOME/.cursor" ]] && echo "$HOME/.cursor/skills|$HOME/.cursor/commands"
  [[ -d "$HOME/.codex" ]] && echo "$HOME/.codex/skills|$HOME/.codex/prompts"
  [[ -d "$HOME/.config/opencode" ]] && echo "$HOME/.config/opencode/skills|$HOME/.config/opencode/commands"
  return 0
}

place() {  # place SRC DEST: copy, keeping a backup of a different existing file
  local src="$1" dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [[ -f "$dest" ]] && ! cmp -s "$src" "$dest"; then
    cp "$dest" "$dest.bak-$(date +%Y%m%d-%H%M%S)"
    say "backed up existing $dest"
  fi
  cp "$src" "$dest"
}

place_tree() {  # place_tree SRC_DIR DEST_DIR: place every file of a skill folder
  local src="$1" dest="$2" f
  while IFS= read -r f; do place "$src/$f" "$dest/$f"; done < <(cd "$src" && find . -type f | sed 's#^\./##')
}

same_tree() { [[ -d "$2" ]] && diff -rq "$1" "$2" >/dev/null 2>&1; }

# Skills. Most go to every agent home; herdr-lead-mods and lead-handover only to Claude Code, because
# they read Claude's pane mods and session transcripts.
SKILLS=(workboard herdr-orchestration opencode-lead herdr-lead-mods lead-handover)
skill_scope() { case "$1" in herdr-lead-mods|lead-handover) echo claude ;; *) echo all ;; esac; }
is_herdr_skill() { [[ "$1" != workboard ]]; }
HERDR_BINS=(oc-run start-opencode-lead herdr-link lead-canary lead-strike lead-handover lead-retire)
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

hook_edit() {  # hook_edit add|remove COMMAND: prints added, removed or unchanged
  python3 - "$CLAUDE_SETTINGS" "$1" "$2" <<'PYHOOK'
import json, os, sys, shutil, time
path, action, cmd = sys.argv[1:4]
data = json.load(open(path)) if os.path.exists(path) else {}
hooks = data.setdefault("hooks", {})
groups = hooks.setdefault("UserPromptSubmit", [])
present = any(h.get("command") == cmd for g in groups for h in g.get("hooks", []))
if action == "add" and not present:
    groups.append({"hooks": [{"type": "command", "command": cmd}]})
elif action == "remove" and present:
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if h.get("command") != cmd]
    hooks["UserPromptSubmit"] = [g for g in groups if g.get("hooks")]
    if not hooks["UserPromptSubmit"]:
        del hooks["UserPromptSubmit"]
else:
    print("unchanged"); sys.exit(0)
if os.path.exists(path):
    shutil.copy(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(data, open(path, "w"), indent=2)
print("added" if action == "add" else "removed")
PYHOOK
}

if [[ "$uninstall" -eq 1 ]]; then
  echo "Uninstalling Workboard"
  for job in "${JOBS[@]}"; do
    if [[ -f "$LAUNCH_DIR/$job.plist" ]]; then
      launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
      rm -f "$LAUNCH_DIR/$job.plist"; say "removed launchd job $job"
    fi
  done
  for name in workboard dashvis md2pdf "${HERDR_BINS[@]}"; do
    if [[ -L "$BIN/$name" && "$(readlink "$BIN/$name")" == "$REPO"/* ]]; then rm -f "$BIN/$name"; say "removed $BIN/$name"; fi
  done
  while IFS='|' read -r skills commands; do
    [[ -z "$skills" ]] && continue
    for sk in "${SKILLS[@]}"; do
      if same_tree "$REPO/skills/$sk" "$skills/$sk"; then rm -rf "$skills/$sk"; say "removed $skills/$sk"; fi
    done
    if cmp -s "$REPO/commands/dashvis.md" "$commands/dashvis.md" 2>/dev/null; then rm -f "$commands/dashvis.md"; say "removed $commands/dashvis.md"; fi
  done < <(agent_targets)
  if [[ -f "$CLAUDE_SETTINGS" ]] && grep -q "$BIN/lead-canary" "$CLAUDE_SETTINGS"; then
    [[ "$(hook_edit remove "$BIN/lead-canary")" == removed ]] && say "removed the lead-canary hook from $CLAUDE_SETTINGS"
  fi
  say "kept $CONFIG, your board data and ~/.herdr-leads"
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "python3 3.9 or newer is required" >&2; exit 1; }

if [[ -n "$remote_host" ]]; then
  [[ -n "$board_dir" ]] || { echo "--remote needs --board-dir (path to agent-workboard on $remote_host)" >&2; exit 2; }
  mode="remote"
else
  mode="host"
fi
echo "Installing Workboard ($([[ $mode == host ]] && echo "board host" || echo "remote machine, board on $remote_host"))"

# 1. Config
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG" || "$force" -eq 1 ]]; then
  owner="${owner:-$(printf '%s' "${USER:-owner}" | tr '[:upper:]' '[:lower:]')}"
  if [[ -z "$owner_name" ]]; then
    owner_name="$( (id -F 2>/dev/null || getent passwd "$USER" 2>/dev/null | cut -d: -f5 | cut -d, -f1) | awk '{print $1}')"
    owner_name="${owner_name:-$owner}"
  fi
  {
    echo "# Workboard settings. Shell syntax: KEY=VALUE. Real environment variables win."
    echo "WB_OWNER=$owner"
    echo "WB_OWNER_NAME=$owner_name"
    echo "# WB_PORT=4747"
    echo "# WB_DATA_DIR=$REPO            # where board.json and attachments live"
    echo "# WB_NOTIFY_CMD=                # command that gets each reply notification on stdin"
    echo "# WB_LAUNCHD_PATTERN=^dev\\.workboard\\.   # launchd jobs shown on the Machines page"
    if [[ "$mode" == remote ]]; then
      echo "WB_BOARD_HOST=$remote_host"
      echo "WB_BOARD_DIR=$board_dir"
      echo "WB_MACHINE=${machine:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
    fi
  } > "$CONFIG"
  say "wrote $CONFIG (owner: $owner)"
else
  say "kept existing $CONFIG (use --force to rewrite)"
fi

# 2. Commands on PATH
mkdir -p "$BIN"
chmod +x "$REPO"/scripts/{workboard.py,workboard-remote,dashvis,md2pdf,serve.py}
if [[ "$mode" == host ]]; then ln -sf "$REPO/scripts/workboard.py" "$BIN/workboard"; else ln -sf "$REPO/scripts/workboard-remote" "$BIN/workboard"; fi
ln -sf "$REPO/scripts/dashvis" "$BIN/dashvis"
ln -sf "$REPO/scripts/md2pdf" "$BIN/md2pdf"
say "linked workboard, dashvis and md2pdf into $BIN"
if [[ "$herdr" -eq 1 ]]; then
  chmod +x "$REPO"/herdr/bin/*
  for name in "${HERDR_BINS[@]}"; do ln -sf "$REPO/herdr/bin/$name" "$BIN/$name"; done
  mkdir -p "$HOME/.herdr-leads"
  say "linked the herdr kit (${HERDR_BINS[*]}) into $BIN"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) say "NOTE: add $BIN to your PATH (e.g. in ~/.zshrc: export PATH=\"\$HOME/.local/bin:\$PATH\")" ;; esac

# 3. Board
if [[ "$mode" == host ]]; then
  if python3 "$REPO/scripts/workboard.py" init ${machine:+--machine "$machine"} $([[ $demo -eq 1 ]] && echo --demo) >/dev/null 2>&1; then
    say "created board.json$([[ $demo -eq 1 ]] && echo " with demo data")"
  else
    say "kept existing board.json"
  fi
else
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$remote_host" "test -f ${board_dir%/}/scripts/workboard.py"; then
    say "reached the board on $remote_host"
    say "on $remote_host, register this machine: workboard machine-add ${machine:-$(hostname -s | tr '[:upper:]' '[:lower:]')} --ssh <its ssh alias from $remote_host>"
  else
    say "WARNING: could not find ${board_dir%/}/scripts/workboard.py on $remote_host over SSH (check keys and path)"
  fi
fi

# 4. Agent skills and /dashvis
if [[ "$agents" -eq 1 ]]; then
  found=0
  while IFS='|' read -r skills commands; do
    [[ -z "$skills" ]] && continue
    installed=()
    for sk in "${SKILLS[@]}"; do
      if is_herdr_skill "$sk" && [[ "$herdr" -eq 0 ]]; then continue; fi
      if [[ "$(skill_scope "$sk")" == claude && "$skills" != "$HOME/.claude/skills" ]]; then continue; fi
      place_tree "$REPO/skills/$sk" "$skills/$sk"
      installed+=("$sk")
    done
    place "$REPO/commands/dashvis.md" "$commands/dashvis.md"
    say "installed /dashvis and skills (${installed[*]}) for ${skills%/skills}"
    found=1
  done < <(agent_targets)
  [[ "$found" -eq 1 ]] || say "no agent folders found (~/.claude, ~/.cursor, ~/.codex, ~/.config/opencode); copy skills/ and commands/ by hand"
fi

# 4b. Lead handover hook (opt-in, Claude Code)
if [[ "$hook" -eq 1 ]]; then
  if [[ "$herdr" -eq 0 ]]; then
    say "skipped --handover-hook: it needs the herdr kit (drop --no-herdr)"
  else
    case "$(hook_edit add "$BIN/lead-canary")" in
      added) say "added the lead-canary UserPromptSubmit hook to $CLAUDE_SETTINGS (backup kept)" ;;
      *) say "lead-canary hook already in $CLAUDE_SETTINGS" ;;
    esac
    say "enable a workspace from inside herdr with: lead-canary --enable [label]"
  fi
fi

# 5. Background jobs (macOS board host)
plist() {  # plist LABEL KEEPALIVE INTERVAL ARGS...
  local label="$1" keepalive="$2" interval="$3"; shift 3
  local data_dir="$REPO"
  printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'
  printf '  <key>Label</key><string>%s</string>\n  <key>ProgramArguments</key>\n  <array>\n' "$label"
  for arg in "$@"; do printf '    <string>%s</string>\n' "$arg"; done
  printf '  </array>\n  <key>WorkingDirectory</key><string>%s</string>\n' "$data_dir"
  printf '  <key>EnvironmentVariables</key><dict><key>PATH</key><string>%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>\n' "$HOME"
  if [[ "$keepalive" == 1 ]]; then printf '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n'; fi
  if [[ -n "$interval" ]]; then printf '  <key>RunAtLoad</key><true/>\n  <key>StartInterval</key><integer>%s</integer>\n' "$interval"; fi
  printf '  <key>StandardOutPath</key><string>%s/%s.log</string>\n  <key>StandardErrorPath</key><string>%s/%s.log</string>\n</dict>\n</plist>\n' \
    "$data_dir" "${label#dev.workboard.}" "$data_dir" "${label#dev.workboard.}"
}

if [[ "$mode" == host && "$launchd" -eq 1 ]]; then
  if [[ "$(uname)" == Darwin ]]; then
    mkdir -p "$LAUNCH_DIR"
    plist dev.workboard.web 1 "" /usr/bin/env python3 "$REPO/scripts/serve.py" > "$LAUNCH_DIR/dev.workboard.web.plist"
    plist dev.workboard.sync 0 600 /usr/bin/env python3 "$REPO/scripts/workboard.py" sync > "$LAUNCH_DIR/dev.workboard.sync.plist"
    for job in "${JOBS[@]}"; do
      launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$LAUNCH_DIR/$job.plist"
    done
    say "started launchd jobs: web server (always on) and sync (every 10 minutes)"
  else
    say "not macOS: start the web server with 'python3 $REPO/scripts/serve.py' (see README for a systemd unit)"
  fi
fi

echo
if [[ "$mode" == host ]]; then
  echo "Done. Open the board: dashvis   (or http://127.0.0.1:${WB_PORT:-4747})"
else
  echo "Done. Try: workboard summary"
fi

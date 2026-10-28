# Changelog

## 1.1.0

The herdr kit: the skills and commands that run agents around the board.

- Skills: `herdr-orchestration` (lead starts, briefs, tracks and verifies workers in herdr tabs,
  with an OpenCode brief template), `opencode-lead` (OpenCode stands in as lead on a local or
  cheap model), `herdr-lead-mods` (worker tier from Claude plan usage) and `lead-handover`
  (tripwires that make a long-running lead hand over to a fresh session).
- Commands: `oc-run`, `start-opencode-lead`, `herdr-link`, `lead-canary`, `lead-strike`,
  `lead-handover`, `lead-retire`.
- `install.sh` installs the kit (`--no-herdr` to skip it) and can add the lead-canary hook to
  Claude Code (`--handover-hook`); `--uninstall` removes both.
- The workboard skill moved from `skill/` to `skills/`.

## 1.0.0

First public release.

- Shared Kanban board for Claude Code, Codex, Cursor, OpenCode and Hermes: `workboard` CLI,
  single-file web board, and an agent skill that tells agents how to use it.
- Needs: agents ask the owner for decisions on the ticket; answers come back through the CLI.
- Replies wake the agent: owner comments are typed into the agent's herdr pane, or the session is
  resumed or a fresh agent started (skip-permissions mode by default, `WB_YOLO=0` to disable).
- Any number of machines over SSH, with live snapshots of agents, dev servers, git activity and
  launchd jobs.
- Workspaces, reply notifications (macOS or any command via `WB_NOTIFY_CMD`), technical-debt
  grades, markdown to PDF attachments.
- `install.sh` for the board host and remote machines; `workboard init --demo` for a demo board.

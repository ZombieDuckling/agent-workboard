---
name: workboard
description: >-
  Shared Kanban board for every coding agent (Claude Code, Codex, Cursor, OpenCode, Hermes)
  across one or more machines. Load when starting multi-step work, or when the user says
  "workboard", "kanban", "board", "what's in progress", "what's running", or "pull up the
  board". Update the board as work moves, not only at the end.
---

# Workboard (shared Kanban for agents)

One `board.json` on one machine (the board host) is the source of truth. Every agent on every
machine reads and writes it through the `workboard` CLI:

- On the board host, `workboard` runs `scripts/workboard.py` directly.
- On other machines, `workboard` is `scripts/workboard-remote`, which forwards the same command
  over SSH to the board host.

Never edit `board.json`, `snapshots.json` or `BOARD.md` by hand: the CLI locks the files so many
agents can write at the same time. If `workboard` is not on PATH, try `~/.local/bin/workboard`.

The person who owns the board is called **the owner** below (their agent id is `WB_OWNER` in
`~/.config/workboard/config.env`; the web UI comments as them).

## When to touch the board

1. **Start of a session:** `workboard needs -a <your-agent-id>` to pick up answers the owner left you.
2. **Start of real work** (not a one-line question): find or create a card, column `in_progress`, `-a` your agent id, `-m` the machine you are on (`workboard machines` lists ids), and a markdown description in `-n`.
3. **While working:** `comment` when something meaningful changes (finding, blocker, scope shift, handoff).
4. **When you need the owner:** leave a `need` (see below), and move the card to `blocked` if you cannot continue.
5. **End of task or session:** attach anything they should read, check `needs` again, then move to `review` or `done`, or back to `ready` if paused.
6. **The owner asks for visibility:** run `dashvis --no-open` (or `workboard sync` then `workboard summary`) and summarise.

## Writing for the owner: markdown, PDFs, needs

The owner reads tickets in the web board, which renders markdown and opens PDFs inside the ticket.

- **Markdown everywhere.** Card descriptions (`-n` on `upsert`), comments and needs are markdown. Keep comments short and specific.
- **PDF for anything they should sit down and read** (report, audit, plan, comparison): write markdown, then `workboard attach CARD_ID report.md --pdf -a <agent> -n "one line on what it is"`. `--pdf` uses `md2pdf` (pandoc and headless Chrome). Mention the attachment in a comment.
- **No PDF when a comment will do:** status updates and short results stay as comments.
- **Needs are how you ask the owner for anything:** a decision, access, approval, a missing file or fact. `workboard need CARD_ID -a <agent> -n "..."`. Say exactly what you need, why, and what you will do with each possible answer. One need per question. They answer in the ticket; `workboard needs -a <agent>` shows answers; after acting run `workboard need-done CARD_ID NEED_ID -a <agent>`.
- **Never** put secrets, tokens, passwords or credentials in a description, comment, need or attachment.

## The owner's comments wake you

When the owner comments on a ticket or answers a need, the board wakes that ticket's agent about 20 s later:

1. Each card remembers the last agent that wrote to it (its herdr pane, terminal and session), captured from your environment.
2. If that agent is still running in herdr, the board types a `[Workboard] <owner> commented on KEY-N ...` message into its pane.
3. If it has exited, the board looks for a running herdr agent with the same session or name, then tries to resume the session in a new tab, then starts a fresh agent in the project's herdr workspace (OpenCode, then Claude Code, Codex, Cursor). Woken agents start in their skip-permissions mode.

When you get that message: run `workboard card KEY-N`, reply on the ticket with `workboard comment` **first**, then do what was asked. Use a stable `-a` id so the board can find you again. Bulk imports that create cards on behalf of other agents must run with `WB_NO_WAKE_CAPTURE=1`.

## Agent identity (`-a`)

| Runtime | Example |
|---------|---------|
| Cursor | `cursor-<short-label>` |
| Claude Code | `claude-<label>`, `claude-lead` |
| herdr worker | your herdr agent name |
| Codex / OpenCode / Hermes | `codex-<label>`, `opencode-<label>`, `hermes` |

## Commands

```bash
workboard show --active-only                 # cards not in backlog/done
workboard show -m buildbox                   # cards on one machine
workboard show -p storefront -a claude       # filter by project and agent
workboard card SHOP-13                       # full ticket (id or KEY-N)

# Create or update a card (prints the card id)
workboard upsert -p storefront -t "Fix cart rounding" -c in_progress \
  -a claude-checkout -m laptop -n "Repro: three discount codes"

workboard comment CARD_ID -a claude-checkout -n "Found it in \`applyDiscounts\`"
workboard move CARD_ID done -a claude-checkout -n "Merged"
workboard touch CARD_ID -a claude-checkout -n "Blocked on API key"
workboard delete CARD_ID

# Attachments (served by the web board, max 25 MB)
workboard attach CARD_ID report.md --pdf -a claude-checkout -n "Audit findings"
workboard detach CARD_ID ATT_ID -a claude-checkout

# Needs
workboard need CARD_ID -a claude-checkout -n "Which Stripe account should test payments use?"
workboard needs -a claude-checkout           # yours; --all includes resolved
workboard need-done CARD_ID NEED_ID -a claude-checkout

# Projects, machines, workspaces
workboard projects                           # project ids as a tree (use for -p)
workboard project-add --id my-app --title "My App" --path ~/code/my-app -m laptop
workboard project-add --id my-app-api --title "API" --parent my-app -m laptop
workboard project-set my-app --status paused --priority high --description "markdown"
workboard project-rm my-app --move-to other-app   # only when the owner asks
workboard machines                           # machine ids
workboard workspaces                         # dashboard workspaces

# Status
workboard summary                            # needs-you, active cards, what is working per machine
workboard sync                               # snapshot every machine and rewrite BOARD.md
workboard debt                               # technical debt per project, worst first
```

Columns: `backlog` → `ready` → `in_progress` → `blocked` | `review` → `done`.
`blocked` needs a note that names the blocker.

## Pull up the board for the owner

`/dashvis` (installed for Claude Code, Cursor, Codex and OpenCode) runs `dashvis`, which syncs
every machine, opens the web board (http://127.0.0.1:4747 by default; other machines get an SSH
tunnel) and prints `workboard summary`. Use `dashvis --no-open` when the owner is not at this
machine. Lead with what needs them (open needs, blocked cards, failed jobs), then cards in
progress, then what is working on each machine, and give the link.

## herdr leads

When spawning workers, upsert one card per worker task with `-a` set to that worker. On
completion, `move` it from the lead; do not rely on the worker unless it has this skill too.

---
name: herdr-orchestration
description: Use when running inside herdr (HERDR_ENV=1) and the work has more than one part, or when the user asks to spin up, start, brief, watch, or orchestrate agents, tabs, "the lead session", OpenCode, Codex, or sibling Claude sessions. Covers choosing the worker kind, launching it in a herdr tab, writing the brief, tracking it on the workboard, monitoring, verifying, and tidying up.
---

# Orchestrating agents in herdr

You are the **lead** session. You plan, write briefs, read reports, verify, and do the few steps that
are risky or need judgement. Workers do the rest in their own herdr tabs. The workboard is where the
work is tracked: one card per task, comments as progress, and the board wakes the right agent when
the user replies.

Confirm the environment first: `test "$HERDR_ENV" = 1`. `$HERDR_WORKSPACE_ID` and `$HERDR_PANE_ID`
identify you. Drive tabs with the `herdr` CLI, never with terminal-app scripting or tmux.
`herdr --skill` prints herdr's own control guide.

## 1. Choose the worker by the job

| Worker | Use for | Cost | Limits |
|---|---|---|---|
| **OpenCode** (`--kind opencode`, local or cheap model) | Mechanical, literal work: run a script and tabulate, apply an exactly specified edit, follow a numbered procedure | Free or cheap | Often no vision. Follows instructions literally. Slow. A local model serves only a few conversations at once. Can fall into thinking loops. |
| **Codex** (`--kind codex`) | Contained engineering that needs real reasoning or vision: audits, wiring fixes, test updates | Paid: keep briefs tight, pre-chew the facts | Sometimes self-updates and exits on launch |
| **Claude** (`--kind claude -- --model <id>`) | Judgement, design, vision, review | Paid | Never use `/model` in a session: it rewrites the global default |
| **You, directly** | Anything hard to undo, anything touching shared or production state, and short command chains that a slow worker would take hours over | | |

Default to the cheapest worker that can do the job. If a chain of slow agents takes longer than
fifteen commands you could run yourself, run them yourself and say so.

## 2. Launch

```sh
P=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "<dir>" --label <label> --no-focus \
     | grep -oE '"pane_id":"[^"]*"' | head -1 | cut -d'"' -f4)
herdr agent start <name> --kind opencode --pane "$P" --timeout 90000
herdr agent start <name> --kind codex    --pane "$P" --timeout 120000 -- --no-daemon -a never -s danger-full-access
herdr agent start <name> --kind claude   --pane "$P" --timeout 60000  -- --model <model-id>
herdr agent prompt <name> "$(cat brief.md)"     # always from a file: the shell expands backticks in inline text
```

- Agent names: lowercase letters, digits, `-`, `_` only. An uppercase name fails silently when output is grepped.
- Set `--cwd` to the folder the worker must stay in. OpenCode asks permission for any path outside it;
  put inputs and outputs inside the cwd, or answer the prompt with `herdr agent send-keys <name> right enter`.
- After `start`, confirm the status reaches `working` after the prompt. If the pane shows a shell prompt
  instead, the agent exited (a self-update, a bad flag): read the pane, then start it again.
- Headless OpenCode without a tab: `oc-run <workdir> <title> <brief.md>` (run it in the background;
  it writes `<brief>.out.txt` and `<brief>.out.jsonl`).
- zsh does not word-split `$var`; use `${=var}` or run loops under bash.

## 3. Write the brief

The brief is the only context the worker has. State the goal, the verified facts, the exact scope, the
output, and the stop conditions. Do the investigation yourself first and hand over conclusions.

**OpenCode briefs: Simplified Technical English (ASD-STE100), very verbose.** See `brief-template.md`.
- Numbered steps. One instruction per sentence. Active voice. One meaning per word. No idioms.
- Exact paths, exact commands, the expected output of each, and what to do when it differs.
- A safety section: what it must not change and which commands it must not use.
- A fixed report layout. Tell it not to guess causes or recommend.
- Prefer: you write the script, the worker runs it and fills the report.

**Track it on the workboard.** Create or pick the card before you brief (`workboard upsert ...`, see
the `workboard` skill). Every brief names the worker's card id and tells it to comment its progress on
that card after each finished step:
`workboard comment <card-id> -a <worker-name> -n "<what is done>"`.
Each write from inside the worker's herdr pane records its wake handle on the card, so a reply from
the user on the board reaches that agent on the machine it runs on.

**Codex / Claude briefs:** goal, facts already verified ("do not re-investigate"), numbered deliverables,
verification commands with expected numbers, rules, and the exact reply you want.

## 4. Monitor without polling

- Wait in the background so you are woken on completion:
  `herdr agent wait <name> --timeout 7200000` inside a background command that then prints the
  tail of the pane (`herdr pane read <pane>`) and the artefacts you expect.
- `herdr agent read <name> --source visible` works while the agent is busy.
- Stalls: a `working` OpenCode agent with no new tool command for about 20 minutes is looping. Interrupt
  with `herdr agent send-keys <name> escape` and re-prompt with a narrower step.
- `blocked` status means a permission prompt or a question: read the pane, answer it. `herdr agent prompt`
  is refused while blocked (`agent_blocked`). For a Codex question, open it with
  `herdr agent send-keys <name> shift+left`, then `herdr pane send-text <pane> "<answer>"` and
  `herdr pane send-keys <pane> enter`. `herdr agent wait` returns on `blocked` too, so check the status
  before assuming the agent finished.
- Workers stop on anything ambiguous. When one stops, read why; a one-line correction usually restarts it.
- The board's Machines page and `workboard summary` show every agent across machines.

## 5. Verify, then land

- Never accept a worker's summary as the result. Read the diff, re-run the checks, look at the output.
- Independent measurements are cheap: give a free agent the same question from another angle and
  cross-check the paid agent's answer.
- Review step for pull requests: a reviewer agent with a fixed checklist posts a verdict; merge only
  on approval. GitHub refuses self-approval, so the verdict goes in the review body.
- Move the card when the work lands (`workboard move <card-id> done`) and comment the evidence.

## 6. Safety rules for workers

- Workers are **read-only** against anything shared: production or shared databases, deploy branches,
  published content, other people's branches. They report the exact change (SQL, command); the lead backs
  up and applies it.
- No `git push --force`, no merges, no deploys, no baseline loosening, unless the brief says so and the
  user asked for it.
- Never put tokens or credentials in a brief or on a card, and tell workers not to print them.
- Do not start a second copy of a singleton daemon or run `herdr server stop` from inside a session.
- Do not run exports or heavy reads on a database while the user is working in the app that uses it.

## 6a. Check plan usage before paid workers

If you are a Claude lead, load the `herdr-lead-mods` skill at the start of a lead session. It reads the
usage-meter and agent-radar lines from your own pane, sets the worker tier from the plan usage, and says
how to install the mods if they are missing. Long-running leads should also load `lead-handover`.

## 7. Keep the workspace tidy

Close a tab as soon as its agent reports and you have what you need: `herdr tab close <tab-id>`. Remove
worktrees and scratch clones when their branch lands. Tell the user what is running in one line each;
relay results, because they do not see worker reports.

## Known pitfalls

- `/model` in any Claude session changes the global default for every new session.
- Closing several Claude tabs at once runs their session-end hooks and can stop shared helpers.
- Stacked pull requests do not auto-close when the stack lands in one push; retarget, then close with a pointer.
- `gh pr merge` refuses drafts: `gh pr ready` first.
- Auto-mode permission classifiers may block reviewing or merging your own pull requests whatever the
  allow rules say; that work needs the user to switch the session's mode.
- Codex shows "Hook failed" on every tool call when a Claude-only plugin is enabled in
  `~/.codex/config.toml`. Disable that plugin there.
- Codex "Selected model is at capacity": the agent goes idle with its work uncommitted. Start a new agent
  in the same worktree with `-m <other model>` and a continuation brief that names the uncommitted files.

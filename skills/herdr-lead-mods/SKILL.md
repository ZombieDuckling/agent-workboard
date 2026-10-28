---
name: herdr-lead-mods
description: For Claude Code lead sessions in herdr. Read plan usage (usage-meter) and running workers (agent-radar) from your own pane at session start, before starting any worker, on each worker wake-up and before every report to the user; set the worker tier from the usage; end reports with the usage line; explain how to install the mods when missing.
---

# Usage and worker mods for herdr leads

Two optional Claude Code plugins from the third-party marketplace
[`hamzafer/claude-code-mods`](https://github.com/hamzafer/claude-code-mods) draw lines above the
prompt of every Claude session:

| Mod | The line shows | Use it to |
|---|---|---|
| `usage-meter` | The plan's 5-hour and 7-day usage as bars (green below 75%, yellow from 75%, red from 90%), when the 5-hour window resets, and this session's cost | Choose the worker tier before you start workers |
| `agent-radar` | One line for each running subagent or background command, with its status, time and current step. `/radar` opens a pane with the detail | Find stuck or finished workers in your own session |

The lines are drawn for the user, not given to you as text. To read them, read your own pane:

```sh
herdr pane read "$HERDR_PANE_ID" --source visible | grep -E '5h|7d|session|✓|running' | tail -15
```

To check another lead or worker, use its pane ID (`herdr pane list`).

## 1. Check that the mods are installed

```sh
claude plugin list 2>&1 | grep -E 'usage-meter|agent-radar'
```

Expected output: both names, each enabled. If one or both are missing, go to section 4. Do not stop the
task for this. Without the mods, work as before and assume usage is in the yellow band.

## 2. When to read the lines (often, not once)

| When | Read | Then |
|---|---|---|
| Session start, and after a handover | usage-meter | Set the tier (table below) and note it in your first message |
| Before you start any worker, and before a fan-out | usage-meter | Choose the worker kind from the tier |
| Each time you wake on a worker or a background command | agent-radar, then usage-meter | A line that is `running` with no new step for 20 min is a stall: deal with it. A ✓ means done: verify it and close its tab. Re-check the tier |
| Before every report to the user | both | End the report with the usage line (format below) |
| Before you hand over or end the session | both | Put the last reading in the handover notes |

agent-radar shows only the subagents and background commands **of your own session** (such as your
`herdr agent wait` jobs). Workers in other herdr tabs appear there only through those wait jobs, so keep
one background wait per worker.

**The usage line.** End every report to the user with:

```
Usage: 5h 42% (resets 16:00) · 7d 30% · session $3.10 · running: 2
```

`running` is the number of lines agent-radar shows as running. If the mods are not on screen yet (the
session has not restarted since the install), write `Usage: mods not loaded yet` and restart at your
next clean boundary or handover.

## 3. Use the usage reading

Set the tier from the 5-hour usage:

| 5-hour usage | What to do |
|---|---|
| Below 75% (green) | Normal: cheap workers for the labour; Claude or Codex only for judgement, vision or design (see `herdr-orchestration`). |
| 75–90% (yellow) | Start no new Claude workers. Use OpenCode only. Do judgement work yourself in this session. |
| 90% or more (red) | Start no new paid workers. Let running workers finish. Write down what is still to do in your notes (and on the workboard cards) so the work can continue after the reset time on the line. Tell the user in one line: the usage, the reset time, and what is paused. |

If the 7-day usage is 90% or more, treat the 5-hour reading as red, whatever it shows, **and hand the
workspace to an OpenCode lead** before the limit stops you: follow the `opencode-lead` skill's
`references/handover.md` (write `~/.herdr-leads/<workspace-id>-handover.md`, run `start-opencode-lead`,
tell the user in one line). A 5-hour red alone does not need a handover; it resets within hours.

The session cost on the line is for this session only. If it is more than $20, say so in your next report.

Do not open `/radar` or another pane in a worker's tab. Read worker tabs with `herdr pane read` only.

## 4. Install the mods when they are missing

Install them **user-wide on each machine where Claude lead sessions run**. One install covers every
Claude session and herdr agent on that machine, because they all share `~/.claude`. Do not use
`--project`; a project install covers one repo only. OpenCode and Codex workers cannot use these mods.

```sh
claude plugin marketplace add hamzafer/claude-code-mods && claude plugin install usage-meter@claude-code-mods && claude plugin install agent-radar@claude-code-mods
```

In auto mode the permission classifier may refuse plugin installs. Do not work around it. Send the user
this one line to paste into the prompt box, `!` included:

```
! claude plugin marketplace add hamzafer/claude-code-mods && claude plugin install usage-meter@claude-code-mods && claude plugin install agent-radar@claude-code-mods
```

After the install, the lines appear in each session only after that session restarts. Running sessions
do not get them. Check again with the command in section 1.

## 5. Only these two mods

Install the other mods from that marketplace only when the user asks. Some make a paid model call after
every turn in every agent, and one cancels `rm -r` and force pushes after a timeout, which breaks agents
that work alone. Third-party plugins update without review: if a mod causes errors or slows sessions,
remove it with `claude plugin uninstall <name>` and tell the user.

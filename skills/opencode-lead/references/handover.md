# Handing a workspace from a Claude lead to an OpenCode lead

For Claude leads. Do this when usage-meter shows the **7-day** usage at 90% or more, or when the user
says the weekly limit is near. (The 5-hour limit resets within hours: just pause, do not hand over.)

## 1. Write the handover file

Path: `~/.herdr-leads/<workspace-id>-handover.md` (for example `~/.herdr-leads/w2-handover.md`).
Write it from checked state: run the commands, do not trust memory. The reader is a literal model, so
write in short plain sentences with exact paths and commands.

```
# Handover: <project> (workspace <id>), <date>

## Goal
<one or two sentences: what the user wants from this workspace>

## Repo
Path: <path>. Branch: <branch>. Last commit: <hash> <message>. Uncommitted: <none / list>.
Pushed: <yes / no - and whether the user allowed a push>.

## Workboard
Cards in flight: <KEY-N title - column - next step>, one per line.

## Done (checked)
- <item> - checked with `<command>` -> <result>

## In progress
- <item> - next exact step: <step>

## Running now
- <tab / agent name> - <what> - <how to read it>

## Next steps, in order
1. <exact step>
2. <exact step>

## Waiting on the user
- <question, with my recommendation>

## Project rules and traps
- <deploy steps, things that broke before, people to ask>
```

## 2. Start the OpenCode lead

```sh
start-opencode-lead <workspace-id> <project-dir> ~/.herdr-leads/<workspace-id>-handover.md
```

Then read its pane after 2 minutes (`herdr pane read <pane> --source recent | tail -30`). It must have
loaded the `opencode-lead` skill and written its 3-line start message. If it stalls, send it one short
correction with `herdr pane send-text`.

## 3. Tell the user, then stop

One line: "Handed <workspace> to an OpenCode lead in tab oc-lead; handover at <path>." Start no new
work. Leave running workers alone.

## Coming back

When Claude is available again, the new Claude lead reads
`~/.herdr-leads/<workspace-id>-opencode-notes.md` (the OpenCode lead's notes) and the workboard cards,
checks the work it did (see `references/verify.md`) before anything is pushed, and takes over.

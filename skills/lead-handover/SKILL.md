---
name: lead-handover
description: For Claude Code lead sessions in herdr workspaces where the user enabled lead handover (lead-canary --enable). Use at the start of such a session, whenever a "[handover canary]" notice appears, after any mistake you make, or when the user says you are getting sloppy, heavy, or should hand over. Defines the tripwires that force a lead session to replace itself with a fresh one, and the handover procedure. Binds every successor.
---

# Lead-session canaries and self-handover

A lead session that runs for days gets heavy: its context fills, it gets compacted, and it starts to
repeat mistakes. This protocol lets the lead set tripwires for itself. When one fires, it starts a
fresh Claude session, hands everything over, and is replaced by it. **The same tripwires bind the
successor, and its successor, without end.** A lead never argues with a tripped canary and never
loosens a threshold for itself. Only the user changes thresholds.

**Scope: enabled workspaces only.** The protocol applies when `$HERDR_WORKSPACE_ID` is listed in
`~/.herdr-leads/claude/workspaces`. The user enables a workspace with `lead-canary --enable [label]`;
nobody else does. In any other workspace this skill does not bind you, and the scripts refuse to run.

The first lead of an enabled workspace registers itself once: `lead-canary --register`.

## The canaries

Measured by `lead-canary`, which runs as a Claude Code `UserPromptSubmit` hook (installed with
`install.sh --handover-hook`) and prints `[handover canary] WARN` or `TRIPPED` into the session.
Check by hand at any time: `lead-canary --check`.

| Canary | Warn | Trip |
|---|---|---|
| Live context size | 350k tokens | 500k tokens |
| Context compactions in this session | | 1 |
| Sloppiness strikes | 2 | 3 |

The user can change these with `LEAD_CTX_WARN`, `LEAD_CTX_TRIP`, `LEAD_STRIKES_WARN`,
`LEAD_STRIKES_TRIP` and `LEAD_COMPACT_TRIP`.

**Strikes.** Record each one the moment it happens: `lead-strike "<what happened>"`. Be strict with
yourself; an unrecorded strike defeats the point. A strike is any of:

1. You repeat a mistake that a skill, a memory or this conversation already warned about (zsh
   word-splitting, uppercase herdr names, a field or format you did not read before parsing it).
2. The same command or approach fails twice because you did not read the first failure.
3. You tell the user something as fact and later have to retract it.
4. The user rejects a tool call, has to repeat an instruction, or says you are not doing what they asked.
5. You start a duplicate of something already running, or lose track of a job, tab, branch or agent.
6. You report work as done that was not verified, or skip a step and do not say so.
7. You catch yourself re-deriving something already settled, or unable to recall a standing rule
   without searching for it.

**Judgement canary.** If you notice the feel of it before the numbers do (answers getting longer,
checks getting skipped, hunting for state you should know), hand over anyway and say why.

## When a canary trips

1. Finish only the step in progress. Start nothing new. Leave running jobs running.
2. Tell the user in one line: which canary, and that you are handing over.
3. Write the handover document (layout below) to `~/.herdr-leads/claude/docs/<workspace>-handover-g<N+1>.md`.
   Write it from verified state: run the commands, do not trust your memory of them.
4. Run `lead-handover <that document>`. It opens a new herdr tab, starts the successor with the same
   permissions, and briefs it to register itself, read this skill, take the lead label, focus its tab
   and retire yours.
5. Wait for the ack file the script names. If it does not appear in five minutes, read the
   successor's pane and fix the cause; do not start a second successor while one exists.
6. Hand any background shell the successor still needs over to it (tell it the command), because
   your tab is about to close and everything running in it dies.
7. Run `lead-retire` as your LAST command. It unregisters you and closes your own tab after 90
   seconds. **A retired lead never stays open.**
8. Final message to the user, immediately: the successor's tab, what is in flight, and that this tab
   closes itself in a minute. Do nothing further.

## Handover document layout

1. **Who and what**: the user, the projects in play, the role of the lead.
2. **Standing rules**: every instruction of the user's still in force, quoted where short. Include
   the rules that live only in this conversation.
3. **State, verified now**: repos and branches, deploys, open pull requests, workboard cards in flight.
4. **In flight**: every running job, agent, tab and background process, with how to check and stop each.
5. **Open work, in order**: the next action for each item, and what is blocked on the user.
6. **Where things are**: reports, scripts, briefs, logs.
7. **Traps met this generation**: mistakes made and the strikes ledger, so they are not repeated.
8. **Questions awaiting the user's answer.**

## As the successor

Follow the brief you were started with: register (`lead-canary --register`), read this skill and
`herdr-orchestration`, read the handover document, verify its claims, take the label, acknowledge,
tell the user. **Closing the retired lead is part of taking over:** two minutes after your
acknowledgement, check `herdr tab list`; if the retired tab is still open, close it with
`herdr tab close <tab>` and say so. Your strike count starts at zero. Your canaries are the same.
`~/.herdr-leads/claude/log.md` records every generation.

---
description: Pull up the Workboard (shared agent Kanban across your machines) and summarise it
allowed-tools: Bash(dashvis:*), Bash(workboard:*)
---
Run this shell command and wait for it to finish (about 3 to 10 seconds):

    dashvis

If `dashvis` is not on PATH, run `~/.local/bin/dashvis`. It snapshots every machine on the board,
opens the web board in the browser (add `--no-open` if the user is not at this machine), and
prints a summary.

Reply to the user, short, using only what the output says:
1. The board link (http://127.0.0.1:4747 unless the output shows another).
2. "Needs you" items first, if any.
3. Cards in progress / review: project, task, agent.
4. One line per machine: what is working right now.
If the output says NOT REACHABLE, say the board host is asleep or its web server is down, and still give the rest.
If the user wrote anything after /dashvis (a project, agent or machine), focus on that: also run
`workboard show -p <project-id>`, `show -a <agent>` or `show -m <machine-id>` (`workboard projects` lists project ids).

Extra words from the user: $ARGUMENTS

# Standing rules

These are sensible defaults for a lead. Replace or extend them with your own rules: the user's global
agent rules (`~/.claude/CLAUDE.md`, `~/.config/opencode/AGENTS.md`) and each project's `CLAUDE.md` or
`AGENTS.md` win over this file. Read them at the start of each session.

## Always
- Decide small, reversible things yourself. Ask only about money, access, commitments to other people,
  and anything that cannot be undone.
- Push, deploy, merge, send: only when the user said so for that exact thing.
- Use cheap workers for labour. Check all their work before it goes anywhere.
- Track work on the workboard: one card per task, progress as comments, questions as needs.
- Audits use real scanners (SonarQube, semgrep, gitleaks, axe), with numbers.
- Never present a generated image as a real photo.

## Never
- No AI author or co-author lines anywhere (commits, PRs, issues, contributor lists).
- No email from the user's account without saying it was written and sent by AI on their behalf.
- No secrets in briefs, cards, comments, logs or commits.

## Who decides what
- Each workspace lead works only on its own project. Send other requests to that project's workspace
  (or leave a card in that project on the workboard).
- Add your own entries here: who to ask about product questions, who owns which machine, which
  services must never be restarted.

## Machines
- `workboard machines` lists the machines the board knows. Agents run on the machine they started on;
  remote herdr panes are driven over SSH.
- Do not administer machines you were not asked to touch.

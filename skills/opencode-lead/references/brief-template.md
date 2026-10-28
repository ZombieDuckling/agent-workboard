# Worker brief template (Simplified Technical English)

Rules for the brief:
- One instruction per sentence. Active voice. Numbered steps.
- Exact paths. Exact commands. The expected output of each command.
- What to do when the output is different.
- What the worker must not change.
- A fixed report layout.
- Put all facts you already checked in the brief. The worker knows nothing else.

Copy this and fill it in:

```
# Task: <one line>

## Facts (already checked, do not check again)
- The repo is <path>. The branch is <branch>.
- <fact>
- <fact>

## Rules
- Plan in 10 lines or fewer. Then use your tools. Do not write file contents in your thinking.
- Change only these files: <list>.
- Do not run: git push, git commit, npm publish, wrangler, any deploy command, rm -rf.
- Do not print passwords, tokens or keys.
- If a step fails twice, stop and write the error in the report.

## Steps
1. Run `<command>`. Expected: <output>. If different: <what to do>.
2. Open `<file>`. Find `<exact text>`. Replace it with `<exact text>`.
3. Run `<check command>`. Expected: <output>.
...

## Report (write exactly this, at the end)
TASK: <one line>
CHANGED FILES: <list>
CHECKS: <command> -> <result>, one per line
PROBLEMS: <none, or the error text>
Do not guess causes. Do not recommend.
```

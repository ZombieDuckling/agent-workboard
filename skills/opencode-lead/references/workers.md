# Workers: start, brief, watch, close

All commands are exact. Copy them. Replace only the words in `<angle brackets>`.

## Start an OpenCode worker (at most 1 at a time)

1. Write the brief to a file inside the project folder, for example `<repo>/.oc-brief-<name>.md`. Use `brief-template.md`.
2. Create a tab and keep its pane ID:

   ```sh
   P=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "<repo>" --label "<name>" --no-focus | grep -oE '"pane_id":"[^"]*"' | head -1 | cut -d'"' -f4); echo "$P"
   ```

   Expected: a pane ID such as `w9:p4A`. Write it in your notes.
3. Start the worker in that pane:

   ```sh
   herdr pane send-text "$P" "cd <repo> && opencode run --auto \"\$(cat .oc-brief-<name>.md)\" 2>&1 | tee .oc-<name>.log
   "
   ```

   The line break before the closing quote is the Enter key. It is needed.
4. After 60 seconds, read the pane: `herdr pane read "$P" --source recent | tail -20`. Expected: tool calls appear. If you see a shell prompt and an error, read the error and fix the command.

Names: lowercase letters, digits and `-` only.

Put the worker's workboard card id in the brief and tell it to comment each finished step:
`workboard comment <card-id> -a <name> -n "<result>"`. Its comments from inside the pane also let the
board wake it when the user replies on the card.

## Watch a worker

- Read it: `herdr pane read "$P" --source recent | tail -30`
- Read its log: `tail -40 <repo>/.oc-<name>.log`
- Do not read it every few seconds. Do your own work, then read it every 5 to 10 minutes.
- **Stall:** no new tool call for 20 minutes = it is looping. Send `herdr pane send-keys "$P" ctrl+c`, then start it again with a smaller brief (one step only).
- **Finished:** the shell prompt is back in the pane and the log ends with its report.

## Check the work (always)

1. `git -C <repo> status --short` and `git -C <repo> diff --stat`
2. Read the full diff of every changed file: `git -C <repo> diff <file>`
3. Run the checks from the brief yourself. Compare your numbers with the worker's numbers.
4. If something is wrong: fix it yourself if it is small, or start the worker again with a brief that names the exact problem.

## Close the worker

Move its card (`workboard move <card-id> review -a opencode-lead -n "<evidence>"`). Then `herdr tab close <tab-id>` (the tab ID is in `herdr tab list --workspace "$HERDR_WORKSPACE_ID"`). Only close tabs you opened. Delete the `.oc-brief-*.md` and `.oc-*.log` files when the work is committed, or keep them out of commits.

## Codex (vision and hard reasoning, scarce)

```sh
P=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "<repo>" --label "<name>" --no-focus | grep -oE '"pane_id":"[^"]*"' | head -1 | cut -d'"' -f4)
herdr agent start <name> --kind codex --pane "$P" --timeout 120000 -- --no-daemon -a never -s danger-full-access
herdr agent prompt <name> "$(cat <brief-file>)"
herdr agent wait <name> --timeout 3600000; herdr pane read "$P" --source recent | tail -40
```

Use Codex for: looking at screenshots and saying what is wrong, a bug you failed to fix twice. Give it the facts you already checked, the exact question, and the reply format you want. One Codex agent at a time.

## Claude workers

Do not start Claude workers. Claude is not available; that is why you are lead.

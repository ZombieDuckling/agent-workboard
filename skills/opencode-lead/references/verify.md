# Checks before anything leaves the machine

Run each check that applies. Write each result (with the number) in your notes and in the report.

## Every code change
1. `git status --short` - only the files you expect.
2. `git diff` - read all of it. No debug code, no secrets, no unrelated changes.
3. The project build: `npm run build` (or the project's own command). Expected: exit code 0.
4. The project tests, if any: `npm test`. Expected: all pass. Write the count.
5. Secrets: `gitleaks dir . --no-banner`. Expected: no new leaks in the files you changed.
6. Attribution: `git log -5 --format=%B | grep -ciE 'co-authored|generated with'`. Expected: `0`.

## Websites (anything that runs in a browser)
1. Run the site locally (`npm run preview` or `npm run dev`) and check with Playwright:
   - console errors: 0
   - links and assets that return 404: 0
   - axe accessibility: 0 serious, 0 critical
2. Business facts (phone numbers, addresses, prices, emails) are unchanged unless the user asked: `git diff | grep -E '^[-+].*(tel:|mailto:|\$ ?[0-9])'` - read each line.
3. You cannot see the page. Write "visual check: not done (no vision)" or start one Codex check with screenshots.

## Code audits
Run SonarQube (`sonar analyze`, or eslint with eslint-plugin-sonarjs) and `semgrep --config p/javascript --config p/security-audit`. Report the numbers. Do not rely on reading alone.

## Before push or deploy (only with the user's word for this exact action)
1. All checks above pass.
2. `git log origin/<branch>..HEAD --oneline` - you know every commit that will go out.
3. After the push or deploy: check the live result (CI status with `gh run list -L 3`, or `curl -sI <url>`).

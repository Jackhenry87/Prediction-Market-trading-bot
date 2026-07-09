---
name: ship
description: Test-gated deploy for this trading bot. Runs the full pytest suite as a hard gate, then commits the change on the feature branch, opens (or updates) a PR, and merges to main ONLY if tests are green. Never merges on a red suite. Use when the user says "ship it", "deploy", "/ship", or after finishing a change that should go live.
---

# /ship — test-gated deploy

This repo runs **live-money trading bots**. Nothing reaches `main` (which the
bots deploy from) unless the test suite is green. Follow these steps in order
and STOP at the first failure.

## 1. Hard test gate (never skip)
Run the full suite:

```bash
python -m pytest -q
```

- **If ANY test fails: STOP.** Do not commit, push, or merge. Show the failing
  tests to the user and ask whether to fix or abort. A red suite never ships.
- Only continue when the suite is fully green. Print the pass count.

## 2. Commit on the feature branch
- Confirm you're on the designated feature branch (`claude/polymarket-trading-bot-w9cp9m`), not `main`.
- Stage only the intended files (never `git add -A` blindly — bot state files
  like `sports_line_history.json` / `account_snapshot.json` must not be swept in).
- Commit with a clear message ending in the required Co-Authored-By / Claude-Session trailers.

## 3. Rebase onto latest main (avoid the squash-divergence conflict)
Because earlier PRs were squash-merged, the branch carries un-squashed copies.
Rebase cleanly before pushing:

```bash
C=$(git rev-parse HEAD)
git fetch origin main -q
# discard any bot-managed state file that blocks the checkout (NOT git clean)
git checkout -- sports_line_history.json account_snapshot.json 2>/dev/null || true
git checkout -B claude/polymarket-trading-bot-w9cp9m origin/main
git cherry-pick "$C"
```

## 4. Open a PR and merge ONLY when green
Prefer the GitHub MCP tools when authorized:
- `create_pull_request` with a body summarising the change + how it was tested.
- If a CI **test** workflow exists, poll `actions_list` until it completes and
  **merge only on success** (`merge_pull_request`, squash). If it fails, STOP
  and report — do not merge.
- This repo currently has **no CI test workflow** (all workflows are bot
  runners), so **step 1's local pytest IS the gate**. With a green local suite
  you may `merge_pull_request` (squash).

If the GitHub MCP token is expired/unavailable, fall back to a direct push to
`main` (the bots push this way; it's allowed) — but ONLY after step 1 is green:

```bash
git push origin HEAD:main
git push -u origin claude/polymarket-trading-bot-w9cp9m --force-with-lease
```

## 5. Report
State what shipped, the test count, the merge SHA/PR, and whether it went via PR
or direct push. If anything was skipped or a run is still pending, say so plainly.

## Guardrails
- Never merge or push to `main` on a failing/absent test result.
- Never commit secrets or bot state files.
- For demo-only changes, keep them demo-scoped; never point a demo change at prod.

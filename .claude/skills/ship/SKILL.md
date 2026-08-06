---
name: ship
description: Test-gated deploy for this trading bot. Runs the full pytest suite as a hard gate, then commits the change on the feature branch, opens (or updates) a PR, and merges to main ONLY if tests are green. Never merges on a red suite. Use when the user says "ship it", "deploy", "/ship", or after finishing a change that should go live.
---

# /ship — test-gated deploy

Nothing reaches `main` unless the test suite is green. Follow these steps in
order and STOP at the first failure.

## 1. Hard test gate (never skip)

```bash
pip install -r requirements.txt -r paperbook/requirements.txt pytest
python -m pytest -q
```

- **If ANY test fails: STOP.** Do not commit, push, or merge. Show the failing
  tests and ask whether to fix or abort. A red suite never ships.
- Both requirement files matter: without `paperbook/requirements.txt` nine
  tests error on import, which looks identical to a real failure.
- Only continue when the suite is fully green. Print the pass count.

## 2. Commit on the feature branch
- Confirm you're on `claude/repo-review-feedback-kz86wt`, not `main`.
- Stage only the intended files. Live bot state (`clv_sports.csv`,
  `CLV_SCOREBOARD.md`, `paper_trades_favorite.csv`) is gitignored and lives on
  the `bot-state` branch — if any of it shows up in `git status`, something is
  wrong; do not force-add it.
- Commit with a clear message ending in the required trailers.

## 3. Push and open a PR
```bash
git push -u origin claude/repo-review-feedback-kz86wt
```
Then `create_pull_request` with a body summarising the change and how it was
tested.

## 4. Merge only when CI is green
`.github/workflows/tests.yml` runs the suite on every PR. Poll `actions_list`
until the `tests` workflow completes and **merge only on success**
(`merge_pull_request`, squash). If it fails, STOP and report.

There is no direct-push-to-main fallback. The previous version of this skill
allowed one, which is how code reached the deploy branch with nothing checking
it. If the GitHub MCP token is unavailable, push the branch and tell the user
to merge — do not push to `main`.

## 5. Report
State what shipped, the test count, the PR/merge SHA, and anything skipped.

## Guardrails
- Never merge or push to `main` on a failing or absent test result.
- Never commit secrets or bot state files.
- The surviving model (`strategy_favorite.py`) is **paper-only**. It must stay
  that way until `CLV_SCOREBOARD.md` shows 100+ scored samples with a positive
  mean CLV. Wiring it to an order path is not a "ship" — raise it with the
  owner first.

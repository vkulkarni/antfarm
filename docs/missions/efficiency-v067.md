# Mission: Efficiency pass — v0.6.7

## Problem

Dogfood run of mission `feat-191-activity` (8 tasks, ~50 min wall
clock) spent roughly **40% of its time on avoidable thrash**:

- Tasks 02 and 04 each rebuilt from scratch because their PR branches
  conflicted with main after a dependency merged. Each rebuild = one
  full Claude Code session (~5 min) + a re-review (~3 min).
- Builders exited on every empty-queue poll and were respawned by a
  supervisor 15s later, losing warmup (#272).
- Soldier polls on a 30s tick, so every review approval waits up to
  30s before merge is attempted.
- Task-06 sat in `done` for 24 min with a `needs_changes` verdict
  that Soldier failed to convert into a kickback (#284).

The system works, but it wastes wall clock and agent tokens on cycles
that deterministic, AI-free mechanisms could eliminate.

## Scope (bundle B+ — P1–P6)

1. **Soldier: rebase-before-kickback.** On merge conflict, attempt a
   deterministic rebase onto `origin/<integration>` and retry the
   merge before kicking back.
2. **Builder: branch from dep's branch, not `main`.** When a task has
   unmerged dependencies, cut its workspace branch from the dep's
   branch instead of `main`.
3. **Reviewer: skip re-review on pure-rebase reharvest.** When a task
   is re-harvested with identical code (only the base changed), carry
   forward the prior `pass` verdict.
4. **Worker: backoff-in-place on empty queue** (fixes #272). Sleep
   and re-poll instead of exiting after a single empty response.
5. **Soldier: event-driven merge trigger.** Subscribe to the `/events`
   SSE stream and react to `review_approved` events within 1s.
6. **Soldier: merge diagnostic events.** Emit `merge_attempted`,
   `merge_skipped`, and `merge_failed` events so operators can see why
   a done task isn't progressing.

Also fixes #284 as part of P1 (ensuring `run_once_with_review` is the
loop being called and kickback fires on `needs_changes`).

## Out of scope

- AI-assisted conflict resolution (deferred).
- Multi-dep branch graph coordination beyond the "one unmerged dep"
  rule (falls back to integration branch).
- Skipping reviews on fresh builds (only pure-rebase reharvests).
- Persistent event log.
- Replacing Soldier polling entirely — poll remains as fallback.

## Approach

### P1 — Soldier rebase-before-kickback

**Where:** `antfarm/core/soldier.py`, inside `attempt_merge`.

**Current behavior:** Any `git merge --ff-only` failure or three-way
conflict immediately returns `MergeResult.FAILED`; caller kicks task
back, superseding the attempt.

**New behavior:** On conflict:

1. `git fetch origin`
2. `git checkout <pr-branch>`
3. `git rebase origin/<integration_branch>`
4. If rebase completes cleanly:
   - `git push --force-with-lease origin <pr-branch>`
   - Re-run the merge attempt (single retry only)
5. If rebase itself hits a conflict:
   - `git rebase --abort`
   - Return `MergeResult.FAILED` — current kickback path

Invariants:
- Deterministic, no AI.
- Still kicks back on real semantic conflicts.
- PR head changes (force-push), which triggers soldier's existing
  re-review freshness check (good — P3 handles the common "same
  diff" case by carrying verdict forward).

**Also fix #284 as part of this task:** verify `require_review=True`
reaches the Soldier constructor at serve startup. Add a regression
test feeding a `needs_changes` verdict and asserting kickback fires
within one tick.

**Tests** (`tests/test_soldier.py`):
- Clean rebase path merges successfully.
- Rebase conflict path kicks back with reason.
- No retry loop — single rebase attempt max.
- `--force-with-lease` is used (never plain `--force`).
- `needs_changes` verdict triggers kickback (closes #284).

### P2 — Builder branches from dep branch

**Where:** `antfarm/core/workspace.py`, inside `create()`.

**Current behavior:** Worktree is created from
`origin/<integration_branch>`.

**New behavior:**

1. Resolve `depends_on` against current task state from colony.
2. For each unmerged dep (status `done` with attempt not yet
   `merged`), collect its branch name from current attempt.
3. Zero unmerged deps → base = `origin/<integration_branch>`.
4. One unmerged dep → base = `origin/<dep-branch>`.
5. Multiple unmerged deps → fall back to integration branch,
   log a warning. Multi-dep branch graphs are out of scope.
6. Create worktree from selected base.

Invariants:
- Branch naming (`feat/<task-id>-<attempt-id>`) unchanged.
- Worktree location unchanged.
- If the dep kicks back later, task built on its branch becomes
  stale; soldier's re-review freshness check handles this.

**Tests** (`tests/test_workspace.py`):
- No deps → integration branch.
- One unmerged dep → dep's attempt branch.
- One merged dep → integration branch.
- Multiple unmerged deps → integration branch with warning.
- Dep has no branch yet → fall back.

### P3 — Skip re-review on pure-rebase reharvest

**Where:** `antfarm/core/soldier.py`, inside `run_once_with_review`,
at the re-ready branch.

**Current behavior:** When parent attempt SHA differs from the
review task's embedded SHA, review task is re-readied and runs
fresh.

**New behavior:** Before re-readying:

1. Compute code diff of new attempt vs. its own merge base with
   `<integration_branch>`:
   `git diff <merge-base-new>..<new-head> -- ':!**/tests/**'`
2. Compute the same for the old attempt at the review's recorded SHA.
3. If diffs are byte-identical after
   `--ignore-all-space` normalization:
   - Rewrite review task's spec with new SHA.
   - Mark review task `done`.
   - Copy old verdict onto new attempt of parent task.
4. Otherwise: re-ready the review task (current behavior).

Invariants:
- Only carries forward `pass` verdicts, never `needs_changes`.
- Test files in diff will break equivalence — by design, err toward
  re-review when tests change.

**Tests** (`tests/test_soldier.py`):
- Identical-diff pass verdict → carried forward.
- Any code change → re-readies.
- Prior verdict `needs_changes` → always re-readies.
- Test-file change only → re-readies.

### P4 — Worker backoff-in-place (#272 fix)

**Where:** `antfarm/core/worker.py`, inside `run()` loop.

**Current behavior:** Empty forage → log and exit.

**New behavior:** Empty forage:

1. Sleep `poll_interval` (default 30s).
2. Re-forage.
3. After `max_empty_polls` consecutive empties (default 10 = 5 min
   idle), exit as today.
4. Reset counter on successful forage.

Configurable:
- `--poll-interval <seconds>` (default 30)
- `--max-empty-polls <N>` (default 10; `1` preserves current
  "exit-on-first-empty" as opt-in)

**Tests** (`tests/test_worker.py`):
- First empty does not exit.
- After `max_empty_polls` empties, exits.
- Successful forage resets counter.
- Exit still deregisters (finally block).

### P5 — Soldier event-driven trigger

**Where:** `antfarm/core/soldier.py`, inside `run()` loop.

**Current behavior:** Fixed `time.sleep(poll_interval)` between ticks.

**New behavior:** Replace sleep with SSE wait:

1. GET `/events?after=<cursor>&timeout=<poll_interval>`.
2. On `review_approved`, `harvested`, or `merge_requested`: wake
   immediately.
3. On timeout (no relevant event): proceed (current behavior).
4. On connection error: fall back to `time.sleep(poll_interval)`.

Depends on #191 being shipped (it is, in 0.6.6).

**Tests** (`tests/test_soldier.py`):
- `review_approved` wakes loop in <1s.
- Timeout still triggers a tick.
- Connection error falls back to sleep.
- Unrelated event types do not wake.

### P6 — Soldier merge diagnostic events

**Where:** `antfarm/core/soldier.py`, around `attempt_merge` and the
merge-queue filter.

**Current behavior:** Soldier emits `harvested`, `kickback`, and
`merged` events, but stays silent when it *skips* a candidate (blocked
dep, missing PR, superseded attempt) or when a merge *starts* or
*fails without kickback*. When a task stalls in `done/`, there's no
signal explaining why.

**New behavior:** Emit three new event types with `actor="soldier"`:

1. `merge_attempted` — fired at the top of `attempt_merge(task)`.
   Detail: `attempt=<id> branch=<pr-branch>`.
2. `merge_skipped` — fired when soldier evaluates a done task and
   chooses not to merge (dep unmerged, no PR, superseded attempt,
   needs-changes verdict). Detail: `reason=<short-code>`.
3. `merge_failed` — fired when `attempt_merge` returns FAILED (before
   the subsequent kickback event). Detail: `reason=<short-code>`.

Invariants:
- Emissions are best-effort; an emit failure never blocks merge logic.
- Reason codes are short, stable strings (e.g., `dep_unmerged`,
  `no_pr`, `superseded`, `needs_changes`, `merge_conflict`,
  `test_failed`, `rebase_failed`). Documented inline for operators.
- Existing `harvested`/`kickback`/`merged` emissions are unchanged.

**Tests** (`tests/test_soldier.py`):
- `attempt_merge` emits `merge_attempted` with branch + attempt id.
- Skipping a done task with unmerged deps emits `merge_skipped`
  with `reason=dep_unmerged`.
- Failed merge emits `merge_failed` before `kickback`.
- Emit failures don't crash the merge loop.

## Acceptance criteria

- [ ] Soldier rebases before kickback; rebase failure still kicks
      back; tests cover both paths.
- [ ] #284 fixed: `needs_changes` verdict reliably triggers kickback.
- [ ] Builder worktrees base on unmerged-dep branches when applicable;
      tests cover zero/one/many-dep cases.
- [ ] Prior `pass` verdicts carry forward on pure-rebase reharvest;
      any code change triggers re-review.
- [ ] Workers survive empty polls with backoff; still exit after
      sustained idleness; #272 can be closed.
- [ ] Soldier reacts to `review_approved` events in <1s; polling
      remains as fallback.
- [ ] `ruff check .` and `pytest tests/ -x -q` pass.
- [ ] Dogfood run of an 8-task mission shows ≥30% wall-clock
      improvement, and zero avoidable rebuilds (all rebuilds
      traceable to real semantic conflicts).

## Files likely touched

- `antfarm/core/soldier.py` — P1, P3, P5
- `antfarm/core/workspace.py` — P2
- `antfarm/core/worker.py` — P4
- `antfarm/core/cli.py` — P4 flags
- `antfarm/core/serve.py` — #284 regression (ensure `require_review`
  flows through)
- `tests/test_soldier.py`, `tests/test_workspace.py`,
  `tests/test_worker.py` — new tests

## References

- Tonight's dogfood telemetry: 8 tasks, ~50 min, 5 rebuilds (01×1,
  02×1, 04×2, 06×1). Only 06×1 was a legitimate design reject; the
  other 4 were merge-base drift and would be eliminated by P1+P2.
- antfarm-ai/antfarm#272 — worker queue-empty exit (closed by P4).
- antfarm-ai/antfarm#284 — Soldier doesn't kickback on
  `needs_changes` (closed by P1 regression work).
- antfarm-ai/antfarm#264 — Soldier's `_reconcile_external_merge`,
  prior art for soldier taking non-trivial deterministic actions.

## Task decomposition (advisory)

| # | Task | Depends on | Complexity |
|---|------|------------|------------|
| 01 | P1: Soldier rebase-before-kickback + #284 fix | — | M |
| 02 | P2: Builder branch-from-dep | — | M |
| 03 | P3: Skip re-review on pure-rebase | 01 | M |
| 04 | P4: Worker backoff (closes #272) | — | S |
| 05 | P5: Soldier event-driven trigger | — | M |
| 06 | CHANGELOG + version bump (0.6.6 → 0.6.7) | 01, 02, 03, 04, 05 | S |

# issue #325: surface task-retry-pattern failures in inbox

When a mission stalls, operators currently must query individual task trails to
understand why. The inbox surfaces stale workers and blocked-by-deps items but
misses the single most useful signal: **which tasks are retrying repeatedly and
what keeps failing.**

This spec adds a doctor check that aggregates retry-pattern failures into
inbox entries so an operator running `antfarm inbox` or opening the TUI
immediately sees:

```
[ERROR] retry_ceiling: Task 'task-123' has failed 3/3 attempts.
  Last failure: tests failed: ImportError while loading conftest ...
  → Task is at the retry ceiling. Inspect trail or unblock via kickback.
[WARNING] retrying: Task 'task-456' has failed 2 of max 3 attempts.
  Last failure: review failed: needs_changes
  → Task may block the mission if next attempt fails.
```

## Acceptance criteria

### 1. New doctor check

- Add `check_retry_patterns(backend: TaskBackend) -> list[Finding]` in
  `antfarm/core/doctor.py`.
- Scan all tasks (ready/active/done/blocked/paused).
- For each task, count attempts whose status is DONE or SUPERSEDED (same
  "finished" count that `kickback` uses).
- Determine the task's effective max_attempts: per-task override wins,
  else the config or function-param default (3).
- If `finished >= effective_max` AND task status is `blocked`: emit
  a **Finding** with severity `error`, check `retry_ceiling`, message
  naming the task, last finished attempt's kickback/failure reason
  (extract from trail — look for the most recent `action_type=="kickback"`
  entry, fall back to the last stderr-containing trail message),
  and the `fix_hint` pointing operators at trail inspection or
  `antfarm kickback`.
- If `finished >= effective_max - 1` AND task is NOT yet blocked: emit
  a **Finding** with severity `warning`, check `retrying`, message
  naming the task, the same last failure reason, and a fix_hint warning
  about blast radius on next failure.
- Skip infra tasks (plan-\*, review-\*, review-plan-\*).

### 2. Register the check with run_doctor

- Add the new check to the list run by `run_doctor(..., fix=...)` so it
  is included in `antfarm doctor` output and in the inbox feed.
- `fix=True` on retry_ceiling / retrying findings is a no-op (these
  findings are informational; auto-fix is NOT appropriate).

### 3. Inbox integration

- `antfarm/core/inbox.py` either already consumes `Finding` objects (then
  no change is required) OR needs a small adapter. Investigate the
  current implementation. If inbox has its own scanner, add a parallel
  scanner that emits `retry_ceiling` and `retrying` items; otherwise
  wire via run_doctor as above.

### 4. Tests

- `tests/test_doctor.py`:
  - `test_check_retry_patterns_flags_retry_ceiling` — task with 3
    superseded attempts and status=blocked → 1 error finding with
    check="retry_ceiling".
  - `test_check_retry_patterns_flags_retrying_at_two_of_three` — task
    with 2 finished attempts + not yet blocked → 1 warning finding with
    check="retrying".
  - `test_check_retry_patterns_ignores_fresh_task` — task with 0
    attempts → no finding.
  - `test_check_retry_patterns_skips_infra_tasks` — plan-\* and review-\*
    tasks with retries do NOT emit findings.
  - `test_check_retry_patterns_extracts_last_failure_reason` — trail has
    `action_type=="kickback"` entry with a message; finding includes
    that message.
- `tests/test_inbox.py` (if applicable): integration test proving
  retry_ceiling findings show up in `antfarm inbox` output.

### 5. Version bump

- Bump `pyproject.toml` `version` from `0.6.7` to `0.6.8`.
- No CHANGELOG is currently maintained in this repo; skip.

### 6. Scope boundaries

- Do NOT modify `kickback` or `recover_stale_task_if_worker_dead` logic.
- Do NOT change inbox rendering unless strictly required.
- Do NOT add auto-fix behavior. These findings are operator-visibility
  only.

## Files likely touched

- `antfarm/core/doctor.py` — new check, registered with run_doctor
- `antfarm/core/inbox.py` — adapter if needed
- `pyproject.toml` — version bump
- `tests/test_doctor.py` — new tests
- `tests/test_inbox.py` — integration test if applicable

## Non-goals

- No new storage layer for failure-history aggregation — read from trail only.
- No UI changes beyond inbox/doctor text output.
- No mission cancellation on retry-ceiling — operator decides.

Closes #325.

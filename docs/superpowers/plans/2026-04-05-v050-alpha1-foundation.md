# v0.5.1 — Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the runtime deterministic and observable — one scheduling brain, explicit lifecycle states, classified failures with retry policy, and operator visibility for stuck work. This is the foundation that v0.5.2 (artifact + review contract) and v0.5.3 (review execution) build on.

**Architecture:** Five PRs in sequence: (1) model foundation with new states and types, (2) lifecycle enforcement in backends and worker, (3) canonical scheduler refactor, (4) failure classification with retry behavior, (5) initial operator inbox. Each PR is independently testable and leaves the system in a working state.

**Tech Stack:** Python 3.12, pytest, ruff, FastAPI, click, httpx, rich

**Spec:** `docs/SPEC_v05.md` (frozen)

---

## Scope

### In (v0.5.1)
- Canonical scheduler (#72) — single scheduling brain
- Task/attempt lifecycle + invariants — new states, transition rules, recovery semantics
- Failure taxonomy + default retry policy (#83)
- Initial inbox surfacing (#81 partial)

### Out (later slices)
- TaskArtifact + ReviewVerdict contract (v0.5.2)
- Soldier artifact + review gating + freshness checks (v0.5.2)
- Review execution — review-as-task flow (v0.5.3 — THE GOAL)
- Review packs (v0.5.2)
- Repo memory (v0.5.4)
- Conflict prevention weighting (v0.5.4)
- Planner (v0.5.5)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `antfarm/core/models.py` | Modify | Add TaskState, AttemptState, FailureType, FailureRecord |
| `antfarm/core/lifecycle.py` | Create | State transition validator — legal transitions only |
| `antfarm/core/backends/base.py` | Modify | Enforce lifecycle transitions in abstract methods |
| `antfarm/core/backends/file.py` | Modify | Implement new states, remove inline scheduling |
| `antfarm/core/scheduler.py` | Modify | Becomes sole scheduling authority |
| `antfarm/core/worker.py` | Modify | HARVEST_PENDING state, failure classification, retry logic |
| `antfarm/core/soldier.py` | Modify | Use failure type for kickback decisions |
| `antfarm/core/inbox.py` | Create | Inbox data collection (reusable by CLI + TUI) |
| `antfarm/core/tui.py` | Modify | Add inbox panel using inbox.py |
| `antfarm/core/cli.py` | Modify | Add `antfarm inbox` command using inbox.py |
| `tests/test_models.py` | Modify | Roundtrip tests for new types |
| `tests/test_lifecycle.py` | Create | Transition legality tests |
| `tests/test_scheduler_integration.py` | Create | Verify pull() delegates to scheduler |
| `tests/test_worker.py` | Modify | Failure classification + retry tests |
| `tests/test_inbox.py` | Create | Inbox surfacing tests |

---

## PR 1: Model Foundation

**Issue:** Alpha.1: Add lifecycle states, FailureType, FailureRecord to models

**Files:**
- Modify: `antfarm/core/models.py`
- Create: `antfarm/core/lifecycle.py`
- Modify: `tests/test_models.py`
- Create: `tests/test_lifecycle.py`

### What changes

Add to `models.py`:

```python
class TaskState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    ACTIVE = "active"
    HARVEST_PENDING = "harvest_pending"
    DONE = "done"
    KICKED_BACK = "kicked_back"
    MERGE_READY = "merge_ready"
    MERGED = "merged"
    FAILED = "failed"
    PAUSED = "paused"


class AttemptState(StrEnum):
    STARTED = "started"
    HEARTBEATING = "heartbeating"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_FAILED = "agent_failed"
    HARVESTED = "harvested"
    STALE = "stale"
    ABANDONED = "abandoned"


class FailureType(StrEnum):
    AGENT_CRASH = "agent_crash"
    AGENT_TIMEOUT = "agent_timeout"
    TEST_FAILURE = "test_failure"
    LINT_FAILURE = "lint_failure"
    MERGE_CONFLICT = "merge_conflict"
    BUILD_FAILURE = "build_failure"
    INFRA_FAILURE = "infra_failure"
    INVALID_TASK = "invalid_task"


@dataclass
class FailureRecord:
    task_id: str
    attempt_id: str
    worker_id: str
    failure_type: FailureType
    message: str
    retryable: bool
    captured_at: str
    stderr_summary: str
    verification_snapshot: dict = field(default_factory=dict)
    recommended_action: str = "kickback"

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "FailureRecord": ...
```

Create `lifecycle.py`:

```python
# Legal task state transitions
LEGAL_TASK_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"claimed", "blocked", "paused"},
    "blocked": {"queued", "paused"},
    "claimed": {"active"},
    "active": {"harvest_pending", "paused"},
    "harvest_pending": {"done", "failed"},
    "done": {"merge_ready", "kicked_back"},
    "kicked_back": {"queued"},
    "merge_ready": {"merged"},
    "merged": set(),  # terminal
    "failed": {"queued"},  # retry path
    "paused": {"queued", "active", "blocked"},
}

def validate_task_transition(from_state: str, to_state: str) -> bool:
    """Return True if transition is legal, False otherwise."""
    return to_state in LEGAL_TASK_TRANSITIONS.get(from_state, set())

def assert_task_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if transition is illegal."""
    if not validate_task_transition(from_state, to_state):
        raise ValueError(
            f"Illegal task transition: {from_state} → {to_state}. "
            f"Legal: {LEGAL_TASK_TRANSITIONS.get(from_state, set())}"
        )


# Legal attempt state transitions
LEGAL_ATTEMPT_TRANSITIONS: dict[str, set[str]] = {
    "started": {"heartbeating", "agent_failed", "stale"},
    "heartbeating": {"agent_succeeded", "agent_failed", "stale"},
    "agent_succeeded": {"harvested"},
    "agent_failed": {"harvested"},  # failure record written at harvest
    "harvested": set(),  # terminal
    "stale": {"abandoned"},
    "abandoned": set(),  # terminal
}

def validate_attempt_transition(from_state: str, to_state: str) -> bool:
    """Return True if transition is legal, False otherwise."""
    return to_state in LEGAL_ATTEMPT_TRANSITIONS.get(from_state, set())

def assert_attempt_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if transition is illegal."""
    if not validate_attempt_transition(from_state, to_state):
        raise ValueError(
            f"Illegal attempt transition: {from_state} → {to_state}. "
            f"Legal: {LEGAL_ATTEMPT_TRANSITIONS.get(from_state, set())}"
        )
```

### Steps

- [ ] **Step 1: Write failing tests for new enums**

```python
# tests/test_models.py additions

def test_task_state_values():
    from antfarm.core.models import TaskState
    assert TaskState.QUEUED.value == "queued"
    assert TaskState.HARVEST_PENDING.value == "harvest_pending"
    assert TaskState.MERGE_READY.value == "merge_ready"
    assert isinstance(TaskState.QUEUED, str)

def test_attempt_state_values():
    from antfarm.core.models import AttemptState
    assert AttemptState.STARTED.value == "started"
    assert AttemptState.STALE.value == "stale"

def test_failure_type_values():
    from antfarm.core.models import FailureType
    assert FailureType.AGENT_CRASH.value == "agent_crash"
    assert FailureType.INVALID_TASK.value == "invalid_task"

def test_failure_record_roundtrip():
    from antfarm.core.models import FailureRecord, FailureType
    rec = FailureRecord(
        task_id="task-001", attempt_id="att-001", worker_id="w1",
        failure_type=FailureType.TEST_FAILURE,
        message="test_auth failed", retryable=False,
        captured_at="2026-04-05T10:00:00Z",
        stderr_summary="AssertionError: expected 200 got 401",
        recommended_action="kickback",
    )
    d = rec.to_dict()
    restored = FailureRecord.from_dict(d)
    assert restored.failure_type == FailureType.TEST_FAILURE
    assert restored.retryable is False
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3.12 -m pytest tests/test_models.py -k "task_state or attempt_state or failure" -v`

- [ ] **Step 3: Implement new enums and FailureRecord in models.py**

- [ ] **Step 4: Write failing tests for lifecycle transitions**

```python
# tests/test_lifecycle.py

from antfarm.core.lifecycle import validate_task_transition, assert_task_transition
import pytest

def test_legal_transition_queued_to_claimed():
    assert validate_task_transition("queued", "claimed") is True

def test_legal_transition_active_to_harvest_pending():
    assert validate_task_transition("active", "harvest_pending") is True

def test_illegal_transition_active_to_merged():
    assert validate_task_transition("active", "merged") is False

def test_illegal_transition_queued_to_done():
    assert validate_task_transition("queued", "done") is False

def test_illegal_transition_failed_to_merged():
    assert validate_task_transition("failed", "merged") is False

def test_assert_raises_on_illegal():
    with pytest.raises(ValueError, match="Illegal task transition"):
        assert_task_transition("active", "merged")

def test_assert_passes_on_legal():
    assert_task_transition("queued", "claimed")  # should not raise

def test_terminal_state_merged_has_no_transitions():
    assert validate_task_transition("merged", "queued") is False
    assert validate_task_transition("merged", "active") is False

# --- Attempt transitions ---

from antfarm.core.lifecycle import validate_attempt_transition, assert_attempt_transition

def test_legal_attempt_started_to_heartbeating():
    assert validate_attempt_transition("started", "heartbeating") is True

def test_legal_attempt_heartbeating_to_succeeded():
    assert validate_attempt_transition("heartbeating", "agent_succeeded") is True

def test_illegal_attempt_started_to_harvested():
    assert validate_attempt_transition("started", "harvested") is False

def test_illegal_attempt_stale_to_succeeded():
    assert validate_attempt_transition("stale", "agent_succeeded") is False

def test_attempt_assert_raises_on_illegal():
    with pytest.raises(ValueError, match="Illegal attempt transition"):
        assert_attempt_transition("started", "harvested")
```

- [ ] **Step 5: Run tests — verify they fail**

- [ ] **Step 6: Implement lifecycle.py**

- [ ] **Step 7: Run all tests — verify pass**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`

- [ ] **Step 8: Commit**

```bash
git add antfarm/core/models.py antfarm/core/lifecycle.py tests/test_models.py tests/test_lifecycle.py
git commit -m "feat(models): add TaskState, AttemptState, FailureType, FailureRecord, lifecycle transitions"
```

---

## PR 2: Lifecycle Enforcement

**Issue:** Alpha.1: Enforce explicit task/attempt lifecycle in backends and worker

**Files:**
- Modify: `antfarm/core/backends/base.py`
- Modify: `antfarm/core/backends/file.py`
- Modify: `antfarm/core/worker.py`
- Modify: `tests/test_file_backend.py`
- Modify: `tests/test_worker.py`

### What changes

- FileBackend state mutations call `assert_task_transition()` before moving files
- Worker sets HARVEST_PENDING before writing artifact/failure
- Worker death → attempt becomes STALE (not silently DONE)
- If harvest write fails, task stays HARVEST_PENDING (surfaced by inbox later)
- Backward compatibility: existing "ready"/"active"/"done" map to new states

### Steps

- [ ] **Step 1: Write failing test — worker crash before harvest = STALE**

```python
# tests/test_worker.py addition

def test_worker_crash_before_harvest_marks_stale(backend, ...):
    """If worker dies mid-task, attempt becomes STALE, not DONE."""
    # Carry + forage task
    # Simulate worker crash (don't call harvest)
    # Run doctor stale recovery
    # Verify attempt status is STALE, not DONE
    # Verify task is re-queued (QUEUED or equivalent)
```

- [ ] **Step 2: Write failing test — illegal transition rejected**

```python
# tests/test_file_backend.py addition

def test_illegal_transition_raises(backend):
    """Backend rejects illegal state transitions."""
    backend.carry(_make_task("task-001"))
    # Task is in QUEUED state — try to merge directly (skipping CLAIMED, ACTIVE, DONE, MERGE_READY)
    with pytest.raises(ValueError, match="Illegal task transition"):
        backend.mark_merged("task-001", "nonexistent-attempt")
```

- [ ] **Step 3: Run tests — verify they fail**

- [ ] **Step 4: Add lifecycle enforcement to FileBackend**

In each state-mutating method (pull, mark_harvested, kickback, mark_merged, pause_task, etc.), add:
```python
from antfarm.core.lifecycle import assert_task_transition
assert_task_transition(current_status, new_status)
```

- [ ] **Step 5: Add HARVEST_PENDING as a real state mutation**

Add `mark_harvest_pending(self, task_id: str, attempt_id: str) -> None` to `backends/base.py` and `file.py`. This transitions `ACTIVE → HARVEST_PENDING` before the artifact/failure is written.

In `_process_one_task()`, after agent finishes but before writing artifact or failure:
```python
# Real state transition — not just a trail message
self.colony._client.post(
    f"/tasks/{task_id}/harvest-pending",
    json={"attempt_id": attempt_id},
)
```

Add `POST /tasks/{task_id}/harvest-pending` endpoint to `serve.py`.

If worker dies between HARVEST_PENDING and DONE/FAILED, the inbox surfaces it as "harvest interrupted — needs manual recovery or re-attempt."

- [ ] **Step 6: Run all tests — verify pass**

- [ ] **Step 7: Write backward-compatibility test for old persisted state**

```python
# tests/test_file_backend.py addition

def test_old_state_loads_with_new_lifecycle(tmp_path):
    """Existing task JSON with old 'ready'/'active'/'done' values loads correctly."""
    import json, os

    data_dir = tmp_path / ".antfarm"
    ready_dir = data_dir / "tasks" / "ready"
    ready_dir.mkdir(parents=True)

    # Write old-format task JSON (uses "ready" not "queued")
    old_task = {
        "id": "task-old", "title": "Old task", "spec": "x",
        "status": "ready",  # old state name
        "complexity": "M", "priority": 10,
        "depends_on": [], "touches": [],
        "capabilities_required": [], "pinned_to": None,
        "merge_override": None, "current_attempt": None,
        "attempts": [], "trail": [], "signals": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "created_by": "test",
    }
    (ready_dir / "task-old.json").write_text(json.dumps(old_task))

    from antfarm.core.backends.file import FileBackend
    backend = FileBackend(root=str(data_dir))

    # Should load without error
    tasks = backend.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-old"

    # Should be pullable
    backend.register_worker({"worker_id": "w1", "node_id": "n1",
                             "agent_type": "generic", "workspace_root": "/tmp",
                             "capabilities": []})
    result = backend.pull("w1")
    assert result is not None
    assert result["id"] == "task-old"
```

- [ ] **Step 8: Run all tests — verify pass**

- [ ] **Step 9: Commit**

```bash
git commit -m "feat(core): enforce task/attempt lifecycle transitions in backends and worker"
```

---

## PR 3: Canonical Scheduler Refactor

**Issue:** Alpha.1: Remove inline scheduling from FileBackend.pull() (#72)

**Files:**
- Modify: `antfarm/core/scheduler.py`
- Modify: `antfarm/core/backends/file.py`
- Create: `tests/test_scheduler_integration.py`

### What changes

- Remove all inline scheduling logic from `file.py` pull() (lines ~195-222)
- pull() reads ready tasks, active tasks, worker info, then calls `scheduler.select_task()`
- scheduler.select_task() is the ONLY place that decides task eligibility
- Active tasks loaded and passed to scheduler for scope preference

### Steps

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_scheduler_integration.py

def test_pull_delegates_to_scheduler(backend):
    """pull() must call scheduler.select_task() — not use inline logic."""
    backend.carry(_make_task("task-001", touches=["api"]))
    backend.carry(_make_task("task-002", touches=["frontend"]))
    backend.register_worker({"worker_id": "w1", "node_id": "n1", "agent_type": "generic", "workspace_root": "/tmp", "capabilities": []})

    with patch("antfarm.core.backends.file.select_task") as mock:
        mock.return_value = None
        backend.pull("w1")
        mock.assert_called_once()

def test_pull_passes_active_tasks_to_scheduler(backend):
    """Scope preference requires active tasks in scheduler input."""
    backend.carry(_make_task("task-001", touches=["api"]))
    backend.carry(_make_task("task-002", touches=["api"]))
    backend.carry(_make_task("task-003", touches=["frontend"]))
    backend.register_worker({"worker_id": "w1", ...})
    backend.register_worker({"worker_id": "w2", ...})

    backend.pull("w1")  # claims task-001 (api), now active
    result = backend.pull("w2")  # should prefer task-003 (frontend) via scope preference
    assert result["id"] == "task-003"

def test_pull_scope_preference_works(backend):
    """Two tasks with different touches — second pull prefers non-overlapping."""
    backend.carry(_make_task("task-api", touches=["api"]))
    backend.carry(_make_task("task-ui", touches=["frontend"]))
    backend.register_worker({"worker_id": "w1", ...})
    backend.register_worker({"worker_id": "w2", ...})

    r1 = backend.pull("w1")
    r2 = backend.pull("w2")
    assert set(r1["touches"]) != set(r2["touches"])
```

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Refactor FileBackend.pull()**

Replace inline scheduling with:
```python
from antfarm.core.scheduler import select_task

# In pull():
# 1. Read ready tasks as Task objects
# 2. Collect done_task_ids
# 3. Collect active tasks (NEW — currently never loaded)
# 4. Read worker capabilities + rate limit
# 5. Call select_task(candidates, done_task_ids, active_tasks, worker_capabilities, worker_id)
# 6. If None, return None
# 7. Create attempt, move file (unchanged)
```

- [ ] **Step 4: Run integration tests — verify pass**

- [ ] **Step 5: Run full suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`

- [ ] **Step 6: Commit**

```bash
git commit -m "refactor(scheduler): remove inline scheduling from FileBackend.pull() #72

pull() now delegates to scheduler.select_task(). Active tasks loaded
and passed for scope preference. Single source of truth."
```

---

## PR 4: Failure Classification + Retry Behavior

**Issue:** Alpha.1: Implement failure taxonomy with retry policy (#83)

**Files:**
- Modify: `antfarm/core/worker.py`
- Modify: `antfarm/core/soldier.py`
- Modify: `tests/test_worker.py`

### What changes

- `classify_failure()` function in worker.py
- Worker writes FailureRecord on failure (not just trail message)
- Default retry policy: INFRA retries, TEST kicks back, INVALID escalates
- Repeated AGENT_CRASH stops after N retries

### Steps

- [ ] **Step 1: Write failing tests for classify_failure**

```python
def test_classify_failure_agent_crash():
    from antfarm.core.worker import classify_failure
    from antfarm.core.models import FailureType
    result = classify_failure(returncode=1, stderr="Segmentation fault", stdout="")
    assert result == FailureType.AGENT_CRASH

def test_classify_failure_test_failure():
    result = classify_failure(returncode=1, stderr="", stdout="FAILED tests/test_foo.py")
    assert result == FailureType.TEST_FAILURE

def test_classify_failure_lint_failure():
    result = classify_failure(returncode=1, stderr="", stdout="ruff check failed")
    assert result == FailureType.LINT_FAILURE

def test_classify_failure_timeout():
    result = classify_failure(returncode=-9, stderr="", stdout="")
    assert result == FailureType.AGENT_TIMEOUT

def test_classify_failure_infra():
    result = classify_failure(returncode=1, stderr="connection refused", stdout="")
    assert result == FailureType.INFRA_FAILURE

def test_classify_failure_ambiguous_error_defaults_to_crash():
    """Generic 'error' without test/lint markers should be AGENT_CRASH, not TEST_FAILURE."""
    result = classify_failure(returncode=1, stderr="error occurred", stdout="")
    assert result == FailureType.AGENT_CRASH

def test_classify_failure_ambiguous_failed_defaults_to_crash():
    """Generic 'failed' without test context should be AGENT_CRASH."""
    result = classify_failure(returncode=1, stderr="operation failed", stdout="")
    assert result == FailureType.AGENT_CRASH

def test_classify_failure_lint_before_test():
    """Lint markers take precedence over generic 'error' even if 'test' appears."""
    result = classify_failure(returncode=1, stderr="", stdout="ruff check: 3 errors in test_file.py")
    assert result == FailureType.LINT_FAILURE
```

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Implement classify_failure()**

```python
def classify_failure(returncode: int, stderr: str, stdout: str) -> FailureType:
    """Classify failure with strict precedence to avoid misclassification.

    Order matters — earlier checks take priority. Lint/build/infra checks
    come before test checks to prevent generic markers like 'error' or
    'failed' from triggering false test-failure classifications.
    """
    combined = (stderr + stdout).lower()

    # 1. Timeout (highest priority — clear signal)
    if returncode in (-9, -15) or "timeout" in combined:
        return FailureType.AGENT_TIMEOUT

    # 2. Infrastructure (clear external failures)
    infra_markers = ["permission denied", "disk full", "connection refused",
                     "network unreachable", "enospc", "eacces"]
    if any(m in combined for m in infra_markers):
        return FailureType.INFRA_FAILURE

    # 3. Lint (check before test — "ruff check: 3 errors in test_file.py" is lint, not test)
    lint_markers = ["ruff", "flake8", "pylint", "mypy", "type error", "lint"]
    if any(m in combined for m in lint_markers):
        return FailureType.LINT_FAILURE

    # 4. Build (check before test — "pip install failed" is build, not test)
    build_markers = ["build failed", "compilation error", "pip install",
                     "modulenotfounderror", "importerror"]
    if any(m in combined for m in build_markers):
        return FailureType.BUILD_FAILURE

    # 5. Test (requires BOTH a test-specific marker AND a failure indicator)
    test_contexts = ["pytest", "unittest", "test_", "tests/", "::test"]
    test_failures = ["failed", "assert", "error"]
    has_test_context = any(m in combined for m in test_contexts)
    has_test_failure = any(m in combined for m in test_failures)
    if has_test_context and has_test_failure:
        return FailureType.TEST_FAILURE

    # 6. Default: agent crash (generic non-zero exit with no recognizable pattern)
    return FailureType.AGENT_CRASH
```

- [ ] **Step 4: Write failing test for retry policy**

```python
def test_infra_failure_is_retryable():
    from antfarm.core.worker import get_retry_policy
    from antfarm.core.models import FailureType
    policy = get_retry_policy(FailureType.INFRA_FAILURE)
    assert policy["retryable"] is True
    assert policy["max_retries"] > 0

def test_test_failure_not_retryable():
    policy = get_retry_policy(FailureType.TEST_FAILURE)
    assert policy["retryable"] is False
    assert policy["action"] == "kickback"

def test_invalid_task_escalates():
    policy = get_retry_policy(FailureType.INVALID_TASK)
    assert policy["retryable"] is False
    assert policy["action"] == "escalate"
```

- [ ] **Step 5: Implement get_retry_policy()**

```python
RETRY_POLICIES = {
    FailureType.INFRA_FAILURE: {"retryable": True, "max_retries": 3, "action": "retry"},
    FailureType.AGENT_CRASH: {"retryable": True, "max_retries": 2, "action": "retry"},
    FailureType.AGENT_TIMEOUT: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.TEST_FAILURE: {"retryable": False, "max_retries": 0, "action": "kickback"},
    FailureType.LINT_FAILURE: {"retryable": False, "max_retries": 0, "action": "kickback"},
    FailureType.BUILD_FAILURE: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.MERGE_CONFLICT: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.INVALID_TASK: {"retryable": False, "max_retries": 0, "action": "escalate"},
}

def get_retry_policy(failure_type: FailureType) -> dict:
    return RETRY_POLICIES.get(failure_type, {"retryable": False, "max_retries": 0, "action": "kickback"})
```

- [ ] **Step 6: Wire into _process_one_task() and soldier**

Worker: on failure, classify → build FailureRecord → check retry policy → retry or trail failure
Soldier: on kickback, include failure_type in trail message

- [ ] **Step 7: Run full suite**

- [ ] **Step 8: Commit**

```bash
git commit -m "feat(core): add failure taxonomy with classify_failure() and retry policy #83"
```

---

## PR 5: Initial Operator Inbox

**Issue:** Alpha.1: Add inbox surfacing for stale/blocked/failed work (#81 partial)

**Files:**
- Modify: `antfarm/core/tui.py`
- Modify: `antfarm/core/cli.py`
- Create: `tests/test_inbox.py`

### What changes

- New inbox panel in TUI showing actionable items
- `antfarm inbox` standalone CLI command
- Each item explains: what happened, why, what to do

### Steps

- [ ] **Step 1: Write failing test for inbox data collection**

```python
# tests/test_inbox.py

def test_inbox_finds_stale_workers(backend):
    """Workers with expired heartbeat appear in inbox."""
    # Register worker, backdate heartbeat
    # Collect inbox items
    # Assert stale worker appears with explanation

def test_inbox_finds_blocked_tasks(backend):
    """Tasks blocked by unmet deps appear in inbox."""
    # Carry task with depends_on=["nonexistent"]
    # Collect inbox items
    # Assert blocked task appears with blocking dep

def test_inbox_finds_failed_tasks(backend):
    """Tasks with FailureRecord appear in inbox."""
    # Carry, pull, fail with FailureRecord
    # Collect inbox items
    # Assert failed task appears with failure type

def test_inbox_empty_when_healthy(backend):
    """No inbox items when everything is healthy."""
    # Carry and pull a task (normal state)
    # Collect inbox items
    # Assert empty
```

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Implement inbox data collector**

Create `antfarm/core/inbox.py` (separate module so CLI + TUI both use it):

```python
"""Inbox data collection — surfaces items needing operator attention."""


def collect_inbox_items(status: dict, tasks: list, workers: list) -> list[dict]:
    """Collect actionable items from colony state.

    Returns list of dicts with: severity, type, message, action, task_id/worker_id.
    """
    items = []
    # Stale workers (heartbeat > TTL)
    # Blocked tasks (deps not met) — show which dep is blocking
    # Failed tasks (has FailureRecord) — show failure type + recommended action
    # Long-running active tasks (duration > threshold)
    # Kicked-back tasks — show why soldier rejected
    # Tasks with signals — show signal content
    # HARVEST_PENDING tasks — interrupted harvest, needs recovery
    return items
```

- [ ] **Step 4: Add inbox panel to TUI**

New panel in `_build_display()` showing inbox items with color coding:
- Red: failed, stale
- Yellow: blocked, long-running
- Blue: kicked-back, needs review

- [ ] **Step 5: Add `antfarm inbox` CLI command**

```python
@main.command()
@COLONY_URL_OPTION
@TOKEN_OPTION
def inbox(colony_url, token):
    """Show items needing operator attention."""
    data = _get(colony_url, "/status/full", token=token)
    items = collect_inbox_items(data["status"], data["tasks"], data["workers"])
    if not items:
        click.echo("Inbox empty — everything healthy.")
        return
    for item in items:
        click.echo(f"[{item['severity']}] {item['type']}: {item['message']}")
        click.echo(f"  → {item['action']}")
```

- [ ] **Step 6: Run full suite**

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(cli): add operator inbox for stale/blocked/failed work #81"
```

---

## PR 6: Integration + Tag

- [ ] **Step 1: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`

- [ ] **Step 2: Dogfood test**

Start colony, carry 3 tasks, run 2 workers. Verify:
- Scheduler is singular (scope preference works)
- Kill one worker mid-task → attempt becomes STALE
- Doctor recovers stale task
- Inbox shows stale worker + recovered task
- Failed agent produces classified FailureRecord

- [ ] **Step 3: Update CHANGELOG**

- [ ] **Step 4: Bump version to 0.5.1**

- [ ] **Step 5: Commit, tag, push**

```bash
git tag v0.5.1
git push origin main --tags
```

- [ ] **Step 6: Sync mini-1**

```bash
ssh mini-1 "cd ~/projects/antfarm && git pull origin main"
```

---

## Definition of Done

v0.5.1 is done when:

- [ ] Scheduler is singular — no backend-specific selection logic remains
- [ ] Lifecycle states exist and are enforced — illegal transitions raise ValueError
- [ ] Every failed attempt produces a classified FailureRecord
- [ ] Retry policy is applied — INFRA retries, TEST kicks back, INVALID escalates
- [ ] Stale recovery works — worker death → STALE attempt → re-queued task
- [ ] Inbox explains blocked / stale / failed states with recommended actions
- [ ] Dogfooding on antfarm repo with 2-3 workers passes
- [ ] All tests pass, ruff clean

---

## What's Next

After v0.5.1 ships, the next implementation plans are:

- **v0.5.2** — Artifact + Review Contract (TaskArtifact, ReviewVerdict, Soldier gating, review packs)
- **v0.5.3** — Review Execution (THE GOAL — close the autonomous loop, review-as-task, no manual review)

These plans will be written after v0.5.1 is complete, since they build on the runtime foundation.

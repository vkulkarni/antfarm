# v0.5.0-alpha.1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate scheduling logic, add structured task output, and classify failures — the foundation for all v0.5 features.

**Architecture:** Three independent changes that share no files (except models.py): (1) refactor FileBackend.pull() to call the canonical scheduler instead of duplicating logic, (2) add TaskArtifact dataclass and wire it through worker→harvest→soldier, (3) add FailureType enum and classification in worker. All backward compatible.

**Tech Stack:** Python 3.12, pytest, ruff, FastAPI, click, httpx

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `antfarm/core/models.py` | Modify | Add TaskArtifact, FailureType |
| `antfarm/core/scheduler.py` | No change | Already correct — becomes single source of truth |
| `antfarm/core/backends/file.py` | Modify | Remove inline scheduling from pull(), call scheduler |
| `antfarm/core/worker.py` | Modify | Build TaskArtifact after agent, classify failures |
| `antfarm/core/soldier.py` | Modify | Gate merge on artifact (tests_passed, lint_clean) |
| `antfarm/core/backends/base.py` | Modify | Update mark_harvested() signature for artifact |
| `antfarm/core/serve.py` | Modify | Pass artifact through harvest endpoint |
| `tests/test_scheduler_integration.py` | Create | Verify pull() uses scheduler |
| `tests/test_models.py` | Modify | Add TaskArtifact + FailureType roundtrip tests |
| `tests/test_worker.py` | Modify | Add artifact collection + failure classification tests |
| `tests/test_soldier.py` | Modify | Add artifact gating tests |

---

## Task 1: Canonical Scheduler — Remove Inline Scheduling from FileBackend (#72)

**Files:**
- Modify: `antfarm/core/backends/file.py:160-242`
- Create: `tests/test_scheduler_integration.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_scheduler_integration.py`:

```python
"""Verify FileBackend.pull() delegates to scheduler.select_task()."""

import pytest
from unittest.mock import patch, MagicMock
from antfarm.core.backends.file import FileBackend
from antfarm.core.models import Task, TaskStatus


def _make_task(task_id="task-001", title="Test", spec="Do something",
               depends_on=None, touches=None, priority=10):
    return {
        "id": task_id,
        "title": title,
        "spec": spec,
        "complexity": "M",
        "priority": priority,
        "depends_on": depends_on or [],
        "touches": touches or [],
        "capabilities_required": [],
        "pinned_to": None,
        "merge_override": None,
        "created_by": "test",
    }


@pytest.fixture
def backend(tmp_path):
    return FileBackend(root=str(tmp_path / ".antfarm"))


def test_pull_delegates_to_scheduler(backend):
    """pull() must call scheduler.select_task() — not use inline logic."""
    backend.carry(_make_task("task-001", touches=["api"]))
    backend.carry(_make_task("task-002", touches=["frontend"]))

    # Register a worker so pull can read capabilities
    backend.register_worker({
        "worker_id": "w1", "node_id": "n1", "agent_type": "generic",
        "workspace_root": "/tmp", "capabilities": [],
    })

    with patch("antfarm.core.backends.file.select_task") as mock_scheduler:
        # Make scheduler return task-002 (not task-001 which would be FIFO default)
        mock_scheduler.return_value = None  # Will be called with Task objects

        result = backend.pull("w1")

        # Verify scheduler was called
        mock_scheduler.assert_called_once()
        call_args = mock_scheduler.call_args

        # Verify it received ready_tasks, done_task_ids, active_tasks
        ready_tasks = call_args[0][0]  # first positional arg
        assert len(ready_tasks) == 2
        assert all(isinstance(t, Task) for t in ready_tasks)


def test_pull_scope_preference_works_via_scheduler(backend):
    """Scope preference (from scheduler) must actually affect pull() results."""
    # Carry two tasks with different touches
    backend.carry(_make_task("task-api", touches=["api"], priority=10))
    backend.carry(_make_task("task-frontend", touches=["frontend"], priority=10))

    backend.register_worker({
        "worker_id": "w1", "node_id": "n1", "agent_type": "generic",
        "workspace_root": "/tmp", "capabilities": [],
    })

    # Pull first task (either is fine)
    result1 = backend.pull("w1")
    assert result1 is not None
    first_touches = result1.get("touches", [])

    # Pull second task — scheduler should prefer non-overlapping scope
    backend.register_worker({
        "worker_id": "w2", "node_id": "n1", "agent_type": "generic",
        "workspace_root": "/tmp2", "capabilities": [],
    })
    result2 = backend.pull("w2")
    assert result2 is not None
    # The second task should have different touches than the first
    second_touches = result2.get("touches", [])
    assert set(first_touches) != set(second_touches), \
        "Scheduler scope preference should pick non-overlapping task"


def test_pull_passes_active_tasks_to_scheduler(backend):
    """pull() must load active tasks and pass them to scheduler for scope preference."""
    backend.carry(_make_task("task-001", touches=["api"]))
    backend.carry(_make_task("task-002", touches=["api"]))
    backend.carry(_make_task("task-003", touches=["frontend"]))

    backend.register_worker({
        "worker_id": "w1", "node_id": "n1", "agent_type": "generic",
        "workspace_root": "/tmp", "capabilities": [],
    })

    # Pull task-001 (now active, touches "api")
    result1 = backend.pull("w1")
    assert result1 is not None

    # Now pull again — scheduler should see task-001 as active
    # and prefer task-003 (touches "frontend") over task-002 (touches "api")
    backend.register_worker({
        "worker_id": "w2", "node_id": "n2", "agent_type": "generic",
        "workspace_root": "/tmp2", "capabilities": [],
    })
    result2 = backend.pull("w2")
    assert result2 is not None
    assert result2["id"] == "task-003", \
        "Should prefer task-003 (frontend) since task-001 (api) is active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_scheduler_integration.py -v`
Expected: FAIL — pull() currently doesn't call scheduler or pass active_tasks

- [ ] **Step 3: Refactor FileBackend.pull() to use scheduler**

Modify `antfarm/core/backends/file.py`. Replace the inline scheduling logic in pull() (approximately lines 195-222) with a call to the canonical scheduler:

```python
# At the top of file.py, add import:
from antfarm.core.scheduler import select_task

# In pull() method, replace the inline filtering block with:

    def pull(self, worker_id: str) -> dict | None:
        """Claim next task. Creates a new attempt. Atomic."""
        with self._lock:
            # 1. Read all ready tasks as Task objects
            ready_dir = self._root / "tasks" / "ready"
            if not ready_dir.exists():
                return None

            candidates = []
            for f in sorted(ready_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    candidates.append(Task.from_dict(data))
                except (json.JSONDecodeError, KeyError):
                    continue

            if not candidates:
                return None

            # 2. Collect done task IDs (for dependency checking)
            done_dir = self._root / "tasks" / "done"
            done_task_ids: set[str] = set()
            if done_dir.exists():
                for f in done_dir.glob("*.json"):
                    done_task_ids.add(f.stem)

            # 3. Collect active tasks (for scope preference)
            active_dir = self._root / "tasks" / "active"
            active_tasks: list[Task] = []
            if active_dir.exists():
                for f in active_dir.glob("*.json"):
                    try:
                        data = json.loads(f.read_text())
                        active_tasks.append(Task.from_dict(data))
                    except (json.JSONDecodeError, KeyError):
                        continue

            # 4. Read worker capabilities
            worker_capabilities: set[str] | None = None
            worker_path = self._worker_path(worker_id)
            if worker_path.exists():
                try:
                    worker_data = json.loads(worker_path.read_text())
                    caps = worker_data.get("capabilities", [])
                    if caps:
                        worker_capabilities = set(caps)
                    # Check rate limit cooldown
                    cooldown = worker_data.get("cooldown_until")
                    if cooldown:
                        from antfarm.core.rate_limiter import is_worker_rate_limited
                        if is_worker_rate_limited(cooldown):
                            return None
                except (json.JSONDecodeError, KeyError):
                    pass

            # 5. Call the canonical scheduler (SINGLE SOURCE OF TRUTH)
            winner = select_task(
                ready_tasks=candidates,
                done_task_ids=done_task_ids,
                active_tasks=active_tasks,
                worker_capabilities=worker_capabilities,
                worker_id=worker_id,
            )

            if winner is None:
                return None

            # 6. Create attempt and move file (existing logic, unchanged)
            # ... rest of the method stays the same ...
```

- [ ] **Step 4: Run integration tests**

Run: `python3.12 -m pytest tests/test_scheduler_integration.py -v`
Expected: All 3 PASS

- [ ] **Step 5: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass (the refactor doesn't change behavior, just consolidates it)

- [ ] **Step 6: Run lint**

Run: `python3.12 -m ruff check antfarm/core/backends/file.py`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add antfarm/core/backends/file.py tests/test_scheduler_integration.py
git commit -m "refactor(scheduler): remove inline scheduling from FileBackend.pull() #72

pull() now delegates to scheduler.select_task() instead of duplicating
dependency, capability, pin, and scope filtering logic. This makes the
scheduler the single source of truth for task selection.

Active tasks are now loaded and passed to the scheduler for scope
preference, which was previously missing from pull()."
```

---

## Task 2: Add FailureType Enum and Classification (#83)

**Files:**
- Modify: `antfarm/core/models.py`
- Modify: `antfarm/core/worker.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write the failing test for FailureType enum**

Add to `tests/test_models.py`:

```python
def test_failure_type_values():
    """FailureType enum has expected values."""
    from antfarm.core.models import FailureType

    assert FailureType.AGENT_CRASH.value == "agent_crash"
    assert FailureType.AGENT_TIMEOUT.value == "agent_timeout"
    assert FailureType.TEST_FAILURE.value == "test_failure"
    assert FailureType.LINT_FAILURE.value == "lint_failure"
    assert FailureType.MERGE_CONFLICT.value == "merge_conflict"
    assert FailureType.BUILD_FAILURE.value == "build_failure"
    assert FailureType.INFRA_FAILURE.value == "infra_failure"
    assert FailureType.INVALID_TASK.value == "invalid_task"
    assert isinstance(FailureType.AGENT_CRASH, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_models.py::test_failure_type_values -v`
Expected: FAIL — FailureType not defined yet

- [ ] **Step 3: Add FailureType enum to models.py**

Add to `antfarm/core/models.py` after WorkerStatus:

```python
class FailureType(StrEnum):
    """Classification of task attempt failures."""
    AGENT_CRASH = "agent_crash"
    AGENT_TIMEOUT = "agent_timeout"
    TEST_FAILURE = "test_failure"
    LINT_FAILURE = "lint_failure"
    MERGE_CONFLICT = "merge_conflict"
    BUILD_FAILURE = "build_failure"
    INFRA_FAILURE = "infra_failure"
    INVALID_TASK = "invalid_task"
```

Also add `failure_type: str | None = None` to the Attempt dataclass, and include it in to_dict()/from_dict().

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_models.py::test_failure_type_values -v`
Expected: PASS

- [ ] **Step 5: Write failing test for failure classification in worker**

Add to `tests/test_worker.py`:

```python
def test_classify_failure_agent_crash():
    """Non-zero exit with no test/lint markers = agent_crash."""
    from antfarm.core.worker import classify_failure
    from antfarm.core.models import FailureType

    result = classify_failure(returncode=1, stderr="Segmentation fault", stdout="")
    assert result == FailureType.AGENT_CRASH


def test_classify_failure_test_failure():
    """Exit with pytest/test failure markers = test_failure."""
    from antfarm.core.worker import classify_failure
    from antfarm.core.models import FailureType

    result = classify_failure(
        returncode=1,
        stderr="",
        stdout="FAILED tests/test_foo.py::test_bar - AssertionError"
    )
    assert result == FailureType.TEST_FAILURE


def test_classify_failure_lint_failure():
    """Exit with ruff/lint markers = lint_failure."""
    from antfarm.core.worker import classify_failure
    from antfarm.core.models import FailureType

    result = classify_failure(
        returncode=1,
        stderr="Found 3 errors",
        stdout="ruff check failed"
    )
    assert result == FailureType.LINT_FAILURE


def test_classify_failure_timeout():
    """Exit code -9 or timeout marker = agent_timeout."""
    from antfarm.core.worker import classify_failure
    from antfarm.core.models import FailureType

    result = classify_failure(returncode=-9, stderr="", stdout="")
    assert result == FailureType.AGENT_TIMEOUT
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python3.12 -m pytest tests/test_worker.py -k "classify_failure" -v`
Expected: FAIL — classify_failure not defined

- [ ] **Step 7: Implement classify_failure() in worker.py**

Add to `antfarm/core/worker.py`:

```python
from antfarm.core.models import FailureType


def classify_failure(returncode: int, stderr: str, stdout: str) -> FailureType:
    """Classify a worker failure based on exit code and output.

    Args:
        returncode: Agent subprocess exit code.
        stderr: Agent stderr output.
        stdout: Agent stdout output.

    Returns:
        FailureType classification.
    """
    combined = (stderr + stdout).lower()

    # Timeout signals
    if returncode in (-9, -15) or "timeout" in combined:
        return FailureType.AGENT_TIMEOUT

    # Test failure markers
    test_markers = ["failed", "error", "assert", "pytest", "unittest", "test_"]
    if any(m in combined for m in test_markers) and "test" in combined:
        return FailureType.TEST_FAILURE

    # Lint failure markers
    lint_markers = ["ruff", "flake8", "pylint", "mypy", "lint", "type error"]
    if any(m in combined for m in lint_markers):
        return FailureType.LINT_FAILURE

    # Build failure markers
    build_markers = ["build failed", "compilation error", "pip install", "npm install",
                     "modulenotfounderror", "importerror"]
    if any(m in combined for m in build_markers):
        return FailureType.BUILD_FAILURE

    # Infrastructure failure markers
    infra_markers = ["permission denied", "disk full", "connection refused",
                     "network", "dns", "enospc", "eacces"]
    if any(m in combined for m in infra_markers):
        return FailureType.INFRA_FAILURE

    # Default: agent crash
    return FailureType.AGENT_CRASH
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_worker.py -k "classify_failure" -v`
Expected: All 4 PASS

- [ ] **Step 9: Wire classify_failure into _process_one_task()**

In `antfarm/core/worker.py`, modify the failure path in `_process_one_task()` (around line 147-160) to classify the failure:

```python
        if result.returncode != 0:
            failure_type = classify_failure(result.returncode, result.stderr, result.stdout)
            logger.warning(
                "agent failed task_id=%s failure_type=%s returncode=%d",
                task_id, failure_type.value, result.returncode,
            )
            self.colony.trail(
                task_id,
                self.worker_id,
                f"[{failure_type.value}] agent exited with code {result.returncode}: "
                f"{result.stderr[:200]}",
            )
            return True
```

- [ ] **Step 10: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 11: Commit**

```bash
git add antfarm/core/models.py antfarm/core/worker.py tests/test_models.py tests/test_worker.py
git commit -m "feat(core): add failure taxonomy with classify_failure() #83

Added FailureType enum (agent_crash, agent_timeout, test_failure,
lint_failure, merge_conflict, build_failure, infra_failure, invalid_task).

Worker now classifies failures based on exit code and output markers,
and includes the failure type in trail messages."
```

---

## Task 3: Add TaskArtifact and Structured Output (#77)

**Files:**
- Modify: `antfarm/core/models.py`
- Modify: `antfarm/core/worker.py`
- Modify: `antfarm/core/backends/base.py`
- Modify: `antfarm/core/backends/file.py`
- Modify: `antfarm/core/serve.py`
- Modify: `antfarm/core/soldier.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_soldier.py`

- [ ] **Step 1: Write failing test for TaskArtifact roundtrip**

Add to `tests/test_models.py`:

```python
def test_task_artifact_roundtrip():
    """TaskArtifact serializes and deserializes correctly."""
    from antfarm.core.models import TaskArtifact

    artifact = TaskArtifact(
        task_id="task-001",
        attempt_id="att-001",
        worker_id="node-1/claude-1",
        branch="feat/task-001",
        pr_url="https://github.com/org/repo/pull/42",
        files_changed=["src/auth.py", "tests/test_auth.py"],
        lines_added=120,
        lines_removed=5,
        tests_run=True,
        tests_passed=True,
        test_output_summary="15 passed in 2.3s",
        lint_clean=True,
        summary="Added JWT auth middleware",
        risks=["Token printed at startup"],
        merge_readiness="ready",
        review_focus=["auth.py: HMAC implementation"],
    )

    d = artifact.to_dict()
    restored = TaskArtifact.from_dict(d)

    assert restored.task_id == "task-001"
    assert restored.files_changed == ["src/auth.py", "tests/test_auth.py"]
    assert restored.tests_passed is True
    assert restored.merge_readiness == "ready"
    assert restored.risks == ["Token printed at startup"]


def test_task_artifact_defaults():
    """TaskArtifact has sensible defaults for optional fields."""
    from antfarm.core.models import TaskArtifact

    artifact = TaskArtifact(
        task_id="task-001",
        attempt_id="att-001",
        worker_id="w1",
        branch="feat/x",
    )

    assert artifact.pr_url is None
    assert artifact.files_changed == []
    assert artifact.lines_added == 0
    assert artifact.tests_run is False
    assert artifact.tests_passed is False
    assert artifact.lint_clean is False
    assert artifact.summary == ""
    assert artifact.risks == []
    assert artifact.merge_readiness == "unknown"
    assert artifact.review_focus == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_models.py::test_task_artifact_roundtrip -v`
Expected: FAIL — TaskArtifact not defined

- [ ] **Step 3: Add TaskArtifact dataclass to models.py**

Add to `antfarm/core/models.py`:

```python
@dataclass
class TaskArtifact:
    """Structured output from a completed task attempt."""
    task_id: str
    attempt_id: str
    worker_id: str
    branch: str
    pr_url: str | None = None

    # What changed
    files_changed: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0

    # What was verified
    tests_run: bool = False
    tests_passed: bool = False
    test_output_summary: str = ""
    lint_clean: bool = False

    # Assessment (AI-generated fields — optional, filled by capable adapters)
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    merge_readiness: str = "unknown"  # "ready", "needs_review", "blocked", "unknown"
    review_focus: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
            "test_output_summary": self.test_output_summary,
            "lint_clean": self.lint_clean,
            "summary": self.summary,
            "risks": list(self.risks),
            "merge_readiness": self.merge_readiness,
            "review_focus": list(self.review_focus),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskArtifact":
        return cls(
            task_id=data["task_id"],
            attempt_id=data["attempt_id"],
            worker_id=data["worker_id"],
            branch=data["branch"],
            pr_url=data.get("pr_url"),
            files_changed=data.get("files_changed", []),
            lines_added=data.get("lines_added", 0),
            lines_removed=data.get("lines_removed", 0),
            tests_run=data.get("tests_run", False),
            tests_passed=data.get("tests_passed", False),
            test_output_summary=data.get("test_output_summary", ""),
            lint_clean=data.get("lint_clean", False),
            summary=data.get("summary", ""),
            risks=data.get("risks", []),
            merge_readiness=data.get("merge_readiness", "unknown"),
            review_focus=data.get("review_focus", []),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_models.py -k "artifact" -v`
Expected: Both PASS

- [ ] **Step 5: Add artifact collection to worker**

Add to `antfarm/core/worker.py` a new method `_collect_artifact()`:

```python
def _collect_artifact(self, task: dict, workspace: str, result: AgentResult) -> dict:
    """Collect structured artifact from completed agent work.

    Gathers git diff stats and returns artifact dict.
    AI-generated fields (summary, risks) are left empty — adapters fill these.
    """
    import subprocess as sp

    artifact = {
        "task_id": task["id"],
        "attempt_id": task["current_attempt"],
        "worker_id": self.worker_id,
        "branch": result.branch,
        "pr_url": "",
        "files_changed": [],
        "lines_added": 0,
        "lines_removed": 0,
        "tests_run": False,
        "tests_passed": result.returncode == 0,
        "test_output_summary": "",
        "lint_clean": False,
        "summary": "",
        "risks": [],
        "merge_readiness": "ready" if result.returncode == 0 else "blocked",
        "review_focus": [],
    }

    # Collect git diff stats
    try:
        diff_stat = sp.run(
            ["git", "diff", "--stat", "HEAD~1"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        )
        if diff_stat.returncode == 0:
            lines = diff_stat.stdout.strip().split("\n")
            # Parse files from diff stat
            for line in lines[:-1]:  # skip summary line
                parts = line.strip().split("|")
                if len(parts) >= 1:
                    artifact["files_changed"].append(parts[0].strip())

            # Parse +/- from summary line
            if lines:
                summary = lines[-1]
                import re
                added = re.search(r"(\d+) insertion", summary)
                removed = re.search(r"(\d+) deletion", summary)
                if added:
                    artifact["lines_added"] = int(added.group(1))
                if removed:
                    artifact["lines_removed"] = int(removed.group(1))
    except (sp.TimeoutExpired, Exception):
        pass  # Best effort — don't fail harvest on stat collection

    return artifact
```

Then modify `_process_one_task()` to call it and pass artifact to harvest:

```python
        # In the success path (after line 163):
        artifact = self._collect_artifact(task, workspace, result)
        try:
            self.colony.harvest(
                task_id, attempt_id, pr="", branch=result.branch,
                artifact=artifact,
            )
```

- [ ] **Step 6: Update mark_harvested in backend interface**

Modify `antfarm/core/backends/base.py` — update mark_harvested signature:

```python
    @abstractmethod
    def mark_harvested(
        self, task_id: str, attempt_id: str, pr: str, branch: str,
        artifact: dict | None = None,
    ) -> None:
        """Transition task to DONE, attempt to DONE. Optionally store artifact."""
        ...
```

Modify `antfarm/core/backends/file.py` — store artifact on the attempt:

```python
    def mark_harvested(self, task_id, attempt_id, pr, branch, artifact=None):
        # ... existing logic ...
        # After setting attempt status to DONE:
        if artifact:
            attempt["artifact"] = artifact
        # ... rest of method ...
```

- [ ] **Step 7: Update colony_client and serve.py**

Modify `antfarm/core/colony_client.py` — add artifact to harvest():

```python
    def harvest(self, task_id, attempt_id, pr, branch, artifact=None):
        payload = {"attempt_id": attempt_id, "pr": pr, "branch": branch}
        if artifact:
            payload["artifact"] = artifact
        r = self._client.post(f"/tasks/{task_id}/harvest", json=payload)
        r.raise_for_status()
```

Modify `antfarm/core/serve.py` — accept artifact in HarvestRequest:

```python
class HarvestRequest(BaseModel):
    attempt_id: str
    pr: str
    branch: str
    artifact: dict | None = None
```

And pass it through in the harvest endpoint handler.

- [ ] **Step 8: Add artifact gating to Soldier**

Modify `antfarm/core/soldier.py` — in `attempt_merge()`, before step 3 (git merge), check artifact:

```python
    def attempt_merge(self, task: dict) -> MergeResult:
        # Check artifact if present
        attempt = self._get_current_attempt(task)
        if attempt:
            artifact = attempt.get("artifact")
            if artifact:
                if artifact.get("merge_readiness") == "blocked":
                    self.last_failure_reason = "Task artifact is marked as blocked"
                    return MergeResult.FAILED
                if artifact.get("tests_run") and not artifact.get("tests_passed"):
                    self.last_failure_reason = "Task artifact shows tests failed"
                    return MergeResult.FAILED

        # ... rest of attempt_merge() unchanged ...
```

- [ ] **Step 9: Write soldier artifact gating test**

Add to `tests/test_soldier.py`:

```python
def test_soldier_skips_merge_when_artifact_blocked(soldier_env):
    """Soldier should not merge tasks with merge_readiness=blocked."""
    soldier, cc, repo_path, origin_path = soldier_env

    # Carry, forage, create branch, harvest with blocked artifact
    cc._client.post("/nodes", json={"node_id": "n1"})
    cc.register_worker("n1/w1", "n1", "generic", "/tmp")
    cc._client.post("/tasks", json={
        "id": "task-blocked", "title": "Blocked task", "spec": "x",
    })
    task = cc.forage("n1/w1")

    # Create a commit on the branch
    _commit_file(repo_path, "blocked.txt", "blocked", "add blocked file")
    _git(["push", "origin", "HEAD"], repo_path)

    # Harvest with blocked artifact
    cc.harvest(
        "task-blocked", task["current_attempt"],
        pr="", branch=f"feat/task-blocked-{task['current_attempt']}",
        artifact={"merge_readiness": "blocked", "tests_passed": False},
    )

    results = soldier.run_once()
    # Should be kicked back, not merged
    assert len(results) == 1
    assert results[0][1] == MergeResult.FAILED
```

- [ ] **Step 10: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 11: Run lint**

Run: `python3.12 -m ruff check .`
Expected: Clean

- [ ] **Step 12: Commit**

```bash
git add antfarm/core/models.py antfarm/core/worker.py antfarm/core/backends/base.py \
    antfarm/core/backends/file.py antfarm/core/colony_client.py antfarm/core/serve.py \
    antfarm/core/soldier.py tests/test_models.py tests/test_worker.py tests/test_soldier.py
git commit -m "feat(core): add structured task output with TaskArtifact #77

Workers now collect git diff stats after agent completion and build a
TaskArtifact with: files_changed, lines_added/removed, tests_passed,
merge_readiness.

Soldier gates merge on artifact: skips tasks with merge_readiness=blocked
or tests_passed=False.

AI-generated fields (summary, risks, review_focus) are left empty in v0.5
— adapters can fill these."
```

---

## Task 4: Final Integration + Tag

- [ ] **Step 1: Run full test suite one final time**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 2: Update CHANGELOG**

Add v0.5.0-alpha.1 section to CHANGELOG.md.

- [ ] **Step 3: Bump version**

Update pyproject.toml to `version = "0.5.0a1"`.

- [ ] **Step 4: Commit and tag**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore: bump version to 0.5.0-alpha.1"
git tag v0.5.0a1
git push origin main --tags
```

- [ ] **Step 5: Sync mini-1**

```bash
ssh mini-1 "export PATH=/opt/homebrew/bin:\$PATH && cd ~/projects/antfarm && git pull origin main"
```

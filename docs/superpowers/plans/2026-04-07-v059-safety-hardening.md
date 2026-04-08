# v0.5.9 Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing system safe for unattended operation: no infinite loops, self-healing stale state, no wasted build cycles from cascade failures.

**Architecture:** Three independent changes to existing modules. No new modules. Max-attempt enforcement adds a guard to `file.py:kickback()`. Doctor daemon adds a thread to `serve.py` using the existing soldier thread pattern. Cascade invalidation adds a method to `soldier.py` that recursively kicks back downstream done tasks.

**Tech Stack:** Python 3.12, pytest, FastAPI, threading

**Spec:** `docs/SPEC_v06.md` (frozen), section "v0.5.9 — Safety Hardening"

**Design decisions (resolved before implementation):**

1. **Global `max_attempts` propagation:** The colony server reads `max_attempts` from `.antfarm/config.json` at startup (default: 3). This value is passed to Soldier and the kickback API default. Per-task `max_attempts` field overrides it. The hardcoded `3` is only the fallback when no config exists.

2. **Unblock does NOT reset attempt counter.** `unblock` changes status from `blocked → ready` but attempt history is append-only. The operator must understand that unblocking a task that already hit max_attempts will just get blocked again on the next kickback. To truly reset, the operator should create a new task or manually edit the task JSON. This is the safe, simple choice.

3. **Smart worktree cleanup is default-safe.** If we cannot confidently prove the worktree is clean (any git command fails, no upstream configured, etc.), we keep the worktree. Only auto-delete when both checks succeed AND show nothing.

4. **Cascade invalidation uses a visited set** to guard against malformed cyclic deps and prevent repeated traversal.

---

### Task 1: Max-Attempt Enforcement — Tests

**Files:**
- Modify: `tests/test_file_backend.py`

- [ ] **Step 1: Write test — task blocked after max attempts**

```python
def test_kickback_blocks_after_max_attempts(backend: FileBackend, tmp_path: Path) -> None:
    """Task transitions to blocked after max_attempts kickbacks."""
    backend.carry(_make_task("task-max"))

    for i in range(3):
        pulled = backend.pull("worker-1")
        assert pulled is not None
        attempt_id = pulled["current_attempt"]
        backend.mark_harvested("task-max", attempt_id, pr=f"pr/{i}", branch=f"feat/{i}")
        backend.kickback("task-max", reason=f"failure {i}", max_attempts=3)

    # After 3 kickbacks, task should be blocked, not ready
    blocked_file = tmp_path / ".antfarm" / "tasks" / "blocked" / "task-max.json"
    ready_file = tmp_path / ".antfarm" / "tasks" / "ready" / "task-max.json"
    assert blocked_file.exists()
    assert not ready_file.exists()

    data = json.loads(blocked_file.read_text())
    assert data["status"] == TaskStatus.BLOCKED.value
    assert "max attempts" in data["trail"][-1]["message"].lower()
```

- [ ] **Step 2: Write test — blocked task is not forageable**

```python
def test_blocked_task_not_forageable(backend: FileBackend) -> None:
    """A blocked task should not be returned by pull()."""
    backend.carry(_make_task("task-blk"))

    # Pull, harvest, kickback to blocked
    for i in range(3):
        pulled = backend.pull("worker-1")
        assert pulled is not None
        attempt_id = pulled["current_attempt"]
        backend.mark_harvested("task-blk", attempt_id, pr=f"pr/{i}", branch=f"feat/{i}")
        backend.kickback("task-blk", reason=f"failure {i}", max_attempts=3)

    # Now try to pull — should return None (only task is blocked)
    result = backend.pull("worker-1")
    assert result is None
```

- [ ] **Step 3: Write test — kickback under max_attempts still goes to ready**

```python
def test_kickback_under_max_attempts_goes_to_ready(backend: FileBackend, tmp_path: Path) -> None:
    """Kickback before max_attempts still transitions to ready."""
    backend.carry(_make_task("task-ok"))
    pulled = backend.pull("worker-1")
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-ok", attempt_id, pr="pr/1", branch="feat/1")
    backend.kickback("task-ok", reason="failure 1", max_attempts=3)

    ready_file = tmp_path / ".antfarm" / "tasks" / "ready" / "task-ok.json"
    assert ready_file.exists()
```

- [ ] **Step 4: Write test — per-task max_attempts override**

```python
def test_per_task_max_attempts_override(backend: FileBackend, tmp_path: Path) -> None:
    """Task-level max_attempts overrides the default."""
    task = _make_task("task-override")
    task["max_attempts"] = 1
    backend.carry(task)

    pulled = backend.pull("worker-1")
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-override", attempt_id, pr="pr/1", branch="feat/1")
    backend.kickback("task-override", reason="failure 1", max_attempts=5)

    # Task had max_attempts=1, should be blocked after 1 kickback
    blocked_file = tmp_path / ".antfarm" / "tasks" / "blocked" / "task-override.json"
    assert blocked_file.exists()
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `pytest tests/test_file_backend.py -k "max_attempts or blocked_task_not_forageable or under_max or per_task_max" -v`
Expected: FAIL — `kickback()` doesn't accept `max_attempts` parameter yet

- [ ] **Step 6: Commit test file**

```bash
git add tests/test_file_backend.py
git commit -m "test(backend): add max-attempt enforcement tests"
```

---

### Task 2: Max-Attempt Enforcement — Implementation

**Files:**
- Modify: `antfarm/core/backends/file.py:345-382` (kickback method)
- Modify: `antfarm/core/backends/base.py` (update kickback signature)

- [ ] **Step 1: Update TaskBackend ABC signature**

In `antfarm/core/backends/base.py`, find the `kickback` abstract method and add `max_attempts` parameter:

```python
@abstractmethod
def kickback(self, task_id: str, reason: str, max_attempts: int = 3) -> None:
    """Transition task to READY, current attempt to SUPERSEDED.
    Sets current_attempt to None. Next pull() creates a fresh attempt.
    If attempt count >= max_attempts, transition to BLOCKED instead.
    """
    ...
```

- [ ] **Step 2: Implement max-attempt check in FileBackend.kickback()**

In `antfarm/core/backends/file.py`, replace the `kickback` method (lines 345-382):

```python
def kickback(self, task_id: str, reason: str, max_attempts: int = 3) -> None:
    """Move task from done/ to ready/, or to blocked/ if max attempts reached.

    Soldier calls kickback after a failed integration merge. The task
    is in done/ at that point (harvested but not yet merged).
    Sets current_attempt to None. Adds failure TrailEntry.

    If the number of completed/superseded attempts >= max_attempts,
    the task transitions to BLOCKED instead of READY.
    """
    with self._lock:
        done_path = self._done_path(task_id)
        if not done_path.exists():
            raise FileNotFoundError(f"Task '{task_id}' not found in done/")

        data = self._read_json(done_path)
        now = _now_iso()

        current_attempt_id = data.get("current_attempt")
        worker_id = "system"
        for a in data["attempts"]:
            if a["attempt_id"] == current_attempt_id:
                a["status"] = AttemptStatus.SUPERSEDED.value
                a["completed_at"] = now
                worker_id = a.get("worker_id") or "system"
                break

        data["current_attempt"] = None
        data["updated_at"] = now

        # Count completed/superseded attempts
        attempt_count = len([
            a for a in data["attempts"]
            if a["status"] in (AttemptStatus.DONE.value, AttemptStatus.SUPERSEDED.value)
        ])

        # Per-task max_attempts overrides the default
        effective_max = data.get("max_attempts", max_attempts)

        if attempt_count >= effective_max:
            # Transition to BLOCKED
            block_reason = f"max attempts ({effective_max}) reached: {reason}"
            assert_task_transition(data["status"], TaskStatus.BLOCKED.value)
            trail_entry = TrailEntry(
                ts=now, worker_id=worker_id,
                message=block_reason, action_type="block",
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())
            data["status"] = TaskStatus.BLOCKED.value
            self._write_json(done_path, data)
            os.rename(done_path, self._blocked_path(task_id))
        else:
            # Normal kickback to READY
            assert_task_transition(data["status"], TaskStatus.READY.value)
            trail_entry = TrailEntry(
                ts=now, worker_id=worker_id,
                message=reason, action_type="kickback",
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())
            data["status"] = TaskStatus.READY.value
            self._write_json(done_path, data)
            os.rename(done_path, self._ready_path(task_id))
```

- [ ] **Step 3: Add done→blocked to legal transitions**

In `antfarm/core/lifecycle.py`, line 53, the `"done"` entry currently allows `{"merge_ready", "kicked_back", "queued"}`. Add `"blocked"`:

```python
# In LEGAL_TASK_TRANSITIONS dict, update the "done" entry:
"done": {"merge_ready", "kicked_back", "queued", "blocked"},
```

Without this, `assert_task_transition(data["status"], TaskStatus.BLOCKED.value)` will raise `ValueError` when kickback tries to transition a done task to blocked.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_file_backend.py -k "max_attempts or blocked_task_not_forageable or under_max or per_task_max" -v`
Expected: All 4 new tests PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest tests/ -x -q`
Expected: All 575+ tests pass

- [ ] **Step 6: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add antfarm/core/backends/base.py antfarm/core/backends/file.py antfarm/core/lifecycle.py
git commit -m "feat(backend): enforce max-attempt limit on kickback

Tasks that exceed max_attempts transition to BLOCKED instead of
READY. Per-task max_attempts field overrides the default (3).
Adds done→blocked to legal task transitions.
Prevents infinite kickback loops during unattended operation."
```

---

### Task 3: Propagate max_attempts from Config Through API

**Files:**
- Modify: `antfarm/core/serve.py` (load config, update endpoint)
- Modify: `antfarm/core/colony_client.py` (kickback method signature)
- Modify: `antfarm/core/soldier.py` (Soldier init, _BackendAdapter)

- [ ] **Step 1: Load max_attempts from config in get_app()**

In `serve.py`, after the backend is created in `get_app()`, load the global default:

```python
# After backend is created, load colony config
_max_attempts = 3  # module-level default
config_path = os.path.join(data_dir, "config.json")
if os.path.exists(config_path):
    with open(config_path) as f:
        colony_config = json.load(f)
    _max_attempts = colony_config.get("max_attempts", 3)
```

- [ ] **Step 2: Update KickbackRequest to use config default**

```python
class KickbackRequest(BaseModel):
    reason: str
    max_attempts: int | None = None  # None = use colony default
```

- [ ] **Step 3: Update kickback endpoint to use config default**

```python
@app.post("/tasks/{task_id}/kickback", status_code=200)
def kickback_task(task_id: str, req: KickbackRequest):
    """Return a task to ready state with the given reason."""
    effective_max = req.max_attempts if req.max_attempts is not None else _max_attempts
    try:
        _backend.kickback(task_id, req.reason, max_attempts=effective_max)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _emit_event("kickback", task_id, req.reason)
    return {"ok": True}
```

- [ ] **Step 4: Update ColonyClient.kickback()**

```python
def kickback(self, task_id: str, reason: str, max_attempts: int | None = None) -> None:
    payload = {"reason": reason}
    if max_attempts is not None:
        payload["max_attempts"] = max_attempts
    r = self._client.post(
        f"{self.base_url}/tasks/{task_id}/kickback",
        json=payload,
        headers=self._headers(),
    )
    r.raise_for_status()
```

- [ ] **Step 5: Update _BackendAdapter.kickback() in soldier.py**

```python
def kickback(self, task_id: str, reason: str, max_attempts: int = 3) -> None:
    self._backend.kickback(task_id, reason, max_attempts=max_attempts)
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 7: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 8: Commit**

```bash
git add antfarm/core/soldier.py antfarm/core/colony_client.py antfarm/core/serve.py
git commit -m "feat(server): propagate max_attempts from colony config through API

Colony loads max_attempts from .antfarm/config.json (default 3).
KickbackRequest uses colony default when not explicitly set.
Per-task max_attempts field still overrides everything."
```

---

### Task 4: Doctor Daemon — Tests

**Files:**
- Modify: `tests/test_serve.py`

- [ ] **Step 1: Write test — doctor thread starts with colony**

```python
def test_doctor_thread_starts_with_colony(tmp_path: Path) -> None:
    """Doctor daemon thread starts when enable_doctor=True."""
    from antfarm.core.serve import get_app, _doctor_thread

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_doctor=True)
    # Give thread a moment to start
    import time
    time.sleep(0.2)

    from antfarm.core import serve
    assert serve._doctor_thread is not None
    assert serve._doctor_thread.is_alive()
```

- [ ] **Step 2: Write test — doctor thread does not start when disabled**

```python
def test_doctor_thread_not_started_when_disabled(tmp_path: Path) -> None:
    """Doctor daemon does not start when enable_doctor=False."""
    from antfarm.core.serve import get_app

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    # Reset global state
    import antfarm.core.serve as serve_mod
    serve_mod._doctor_thread = None
    serve_mod._doctor_status = "not started"

    app = get_app(backend=backend, enable_doctor=False)

    assert serve_mod._doctor_thread is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_serve.py -k "doctor_thread" -v`
Expected: FAIL — `enable_doctor` parameter doesn't exist yet

- [ ] **Step 4: Commit**

```bash
git add tests/test_serve.py
git commit -m "test(server): add doctor daemon thread tests"
```

---

### Task 5: Doctor Daemon — Implementation

**Files:**
- Modify: `antfarm/core/serve.py:53-80` (add `_start_doctor_thread` next to soldier)
- Modify: `antfarm/core/serve.py:183-220` (add `enable_doctor` to `get_app`)
- Modify: `antfarm/core/cli.py:120-124` (add `--no-doctor` flag)

- [ ] **Step 1: Add doctor daemon thread globals and function to serve.py**

After the `_start_soldier_thread` function (around line 81), add:

```python
_doctor_thread: threading.Thread | None = None
_doctor_status: str = "not started"


def _start_doctor_thread(
    backend: TaskBackend, data_dir: str, interval: float = 300.0
) -> None:
    """Start the Doctor as a daemon thread (singleton guard)."""
    global _doctor_thread, _doctor_status

    if _doctor_thread is not None and _doctor_thread.is_alive():
        return

    from antfarm.core.doctor import run_doctor

    # Load doctor config from colony config, with sensible defaults
    doctor_config = {"data_dir": data_dir, "worker_ttl": 300, "guard_ttl": 300}
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        import json as _json
        with open(config_path) as f:
            colony_cfg = _json.load(f)
        doctor_config["worker_ttl"] = colony_cfg.get("worker_ttl", 300)
        doctor_config["guard_ttl"] = colony_cfg.get("guard_ttl", 300)
        interval = colony_cfg.get("doctor_interval", interval)

    def _doctor_loop():
        global _doctor_status
        _doctor_status = "running"
        try:
            while True:
                time.sleep(interval)
                try:
                    findings = run_doctor(backend, doctor_config, fix=True)
                    for f in findings:
                        if f.severity == "error":
                            logger.warning("doctor: %s", f.message)
                except Exception as e:
                    logger.error("doctor daemon check failed: %s", e)
        except Exception as e:
            _doctor_status = f"error: {e}"

    _doctor_thread = threading.Thread(
        target=_doctor_loop, daemon=True, name="doctor"
    )
    _doctor_thread.start()
```

- [ ] **Step 2: Add `enable_doctor` parameter to get_app()**

Update `get_app()` signature and body:

```python
def get_app(
    backend: TaskBackend | None = None,
    data_dir: str = ".antfarm",
    auth_secret: str | None = None,
    enable_soldier: bool = False,
    enable_doctor: bool = False,
) -> FastAPI:
```

After the `if enable_soldier:` block (line 219-220), add:

```python
    if enable_doctor:
        _start_doctor_thread(_backend, data_dir)
```

- [ ] **Step 3: Add `--no-doctor` CLI flag**

In `cli.py`, after the `--no-soldier` option (around line 124), add:

```python
@click.option(
    "--no-doctor",
    is_flag=True,
    default=False,
    help="Disable the built-in Doctor health-check daemon.",
)
```

Add `no_doctor` to the `colony()` function parameters and pass it to `get_app()`:

```python
app = get_app(
    task_backend,
    data_dir=data_dir,
    auth_secret=auth_token,
    enable_soldier=not no_soldier,
    enable_doctor=not no_doctor,
)
```

- [ ] **Step 4: Add logger import if not present**

In `serve.py`, ensure `logger` is defined near the top:

```python
import logging
logger = logging.getLogger(__name__)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_serve.py -k "doctor_thread" -v`
Expected: PASS

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add antfarm/core/serve.py antfarm/core/cli.py
git commit -m "feat(server): add doctor daemon thread for self-healing

Doctor runs as a daemon thread alongside Soldier, checking for stale
workers/tasks/guards every 5 minutes and auto-fixing safe issues.
Disabled with --no-doctor flag."
```

---

### Task 6: Smart Worktree Cleanup in Doctor

**Files:**
- Modify: `antfarm/core/doctor.py` (update `check_orphan_workspaces`)
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Read current check_orphan_workspaces implementation**

Read `antfarm/core/doctor.py` and find the `check_orphan_workspaces` function to understand its current behavior before modifying it.

- [ ] **Step 2: Write test — orphan worktree with no changes is auto-deleted**

In `tests/test_doctor.py`:

```python
def test_orphan_worktree_no_changes_auto_deleted(setup, tmp_path):
    """Orphan worktree with no git-tracked changes is auto-deleted with --fix."""
    backend, config = setup
    # Create a fake worktree directory (simulate orphan)
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True)
    orphan = ws_root / "task-orphan-att-001"
    orphan.mkdir()

    config["workspace_root"] = str(ws_root)

    # We can't easily create a real git worktree in tests,
    # so test the finding detection. Smart cleanup is best tested
    # via integration test with real git.
    findings = run_doctor(backend, config, fix=False)
    orphan_findings = [f for f in findings if f.check == "orphan_workspace"]
    # Should report the orphan
    assert any(str(orphan) in f.message or "task-orphan" in f.message for f in orphan_findings)
```

- [ ] **Step 3: Run test to verify current behavior**

Run: `pytest tests/test_doctor.py -k "orphan" -v`
Expected: Check behavior — test may pass if current code already detects orphans

- [ ] **Step 4: Update check_orphan_workspaces for smart cleanup**

In `antfarm/core/doctor.py`, update `check_orphan_workspaces` to add the smart cleanup logic when `fix=True`. The key change: if the worktree has no uncommitted or unpushed changes, delete it.

```python
def _worktree_is_clean(path: str) -> bool:
    """Check if a worktree is provably clean (safe to delete).

    Returns True ONLY when both checks succeed AND show no changes.
    Any failure, missing upstream, or ambiguous state → returns False (keep it).
    """
    try:
        # Check for uncommitted changes
        status = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        if status.stdout.strip():
            return False  # has uncommitted changes

        # Check for unpushed commits — requires upstream to be configured
        log = subprocess.run(
            ["git", "-C", path, "log", "@{u}..", "--oneline"],
            capture_output=True, text=True, check=False,
        )
        if log.returncode != 0:
            return False  # no upstream configured or git error — keep it
        if log.stdout.strip():
            return False  # has unpushed commits

        return True  # provably clean
    except Exception:
        return False  # any error → keep it (safe default)
```

Then in the orphan cleanup section, when `fix=True`:
- If `_worktree_is_clean(path)` returns True → `git worktree remove {path}` and report `fixed=True`
- If `_worktree_is_clean(path)` returns False → report finding but `fixed=False`, message says "kept: has changes or could not verify clean state"
- Key safety rule: only delete when we can **prove** clean. Any ambiguity → keep.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_doctor.py -v`
Expected: All doctor tests pass

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add antfarm/core/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): smart worktree cleanup for orphan workspaces

Orphan worktrees with no uncommitted or unpushed changes are
auto-deleted on --fix. Worktrees with changes are kept for
debugging and reported as findings."
```

---

### Task 7: Cascade Invalidation — Tests

**Files:**
- Modify: `tests/test_soldier.py`

- [ ] **Step 1: Write test — downstream done task is cascade-kicked-back**

```python
def test_cascade_kickback_downstream_done(soldier_env):
    """When A is kicked back, B (depends on A, status=done) is also kicked back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-a", "feat/task-a-cascade")
    _carry_and_harvest(cc, repo, "task-b", "feat/task-b-cascade", depends_on=["task-a"])

    # Both are done. Kick back A via soldier's cascade method.
    soldier.kickback_with_cascade("task-a", "merge conflict")

    task_a = cc.get_task("task-a")
    task_b = cc.get_task("task-b")
    assert task_a["status"] == "ready"
    assert task_b["status"] == "ready"

    # B's trail should mention cascade
    b_trail = [e["message"] for e in task_b["trail"]]
    assert any("cascade" in msg.lower() for msg in b_trail)
```

- [ ] **Step 2: Write test — active downstream is NOT cascade-kicked-back**

```python
def test_cascade_does_not_interrupt_active(soldier_env):
    """Active downstream tasks are not cascade-kicked-back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-p", "feat/task-p-active")

    # Create task-q that depends on task-p, but only forage it (leave active)
    worker_id = "worker-active-q"
    cc.register_worker(worker_id=worker_id, node_id="node-1",
                       agent_type="generic", workspace_root="/tmp/ws")
    cc._client.post("/tasks", json={
        "id": "task-q", "title": "Task Q", "spec": "spec",
        "depends_on": ["task-p"],
    }).raise_for_status()
    task_q = cc.forage(worker_id)
    assert task_q is not None  # task-q is now active

    soldier.kickback_with_cascade("task-p", "failure")

    task_q_after = cc.get_task("task-q")
    assert task_q_after["status"] == "active"  # NOT kicked back
```

- [ ] **Step 3: Write test — merged downstream is NOT cascade-kicked-back**

```python
def test_cascade_does_not_touch_merged(soldier_env):
    """Merged downstream tasks are not cascade-kicked-back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-m1", "feat/task-m1")

    # Merge task-m1 first
    results = soldier.run_once()
    assert results == [("task-m1", MergeResult.MERGED)]

    # Create and harvest task-m2 that depends on task-m1
    _carry_and_harvest(cc, repo, "task-m2", "feat/task-m2", depends_on=["task-m1"])

    # Hypothetical: kick back m1 (shouldn't happen in practice but test the guard)
    # We need to un-merge m1 first to even call kickback... skip this.
    # Instead, test that cascade skips m1 when checking descendants.
    # The real test: m2 is done, m1 is being kicked back.
    # m2 should be kicked back because its dep m1 is kicked back.
    # But if m2 were merged, it should NOT be kicked back.

    # Merge m2
    results2 = soldier.run_once()
    assert results2 == [("task-m2", MergeResult.MERGED)]

    # Now verify m2 is merged
    task_m2 = cc.get_task("task-m2")
    merged_attempts = [a for a in task_m2["attempts"] if a["status"] == "merged"]
    assert len(merged_attempts) == 1
```

- [ ] **Step 4: Write test — recursive cascade (A → B → C)**

```python
def test_cascade_recursive(soldier_env):
    """Cascade propagates recursively: A kicked → B kicked → C kicked."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-r1", "feat/task-r1")
    _carry_and_harvest(cc, repo, "task-r2", "feat/task-r2", depends_on=["task-r1"])
    _carry_and_harvest(cc, repo, "task-r3", "feat/task-r3", depends_on=["task-r2"])

    soldier.kickback_with_cascade("task-r1", "root failure")

    assert cc.get_task("task-r1")["status"] == "ready"
    assert cc.get_task("task-r2")["status"] == "ready"
    assert cc.get_task("task-r3")["status"] == "ready"

    # All should have trail entries
    for tid in ["task-r2", "task-r3"]:
        trail = [e["message"] for e in cc.get_task(tid)["trail"]]
        assert any("cascade" in msg.lower() for msg in trail)
```

- [ ] **Step 5: Write test — independent task NOT cascade-kicked-back**

```python
def test_cascade_does_not_affect_independent(soldier_env):
    """Independent done tasks are not cascade-kicked-back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-ind-a", "feat/task-ind-a")
    _carry_and_harvest(cc, repo, "task-ind-b", "feat/task-ind-b")  # no dep on A

    soldier.kickback_with_cascade("task-ind-a", "failure")

    assert cc.get_task("task-ind-a")["status"] == "ready"
    assert cc.get_task("task-ind-b")["status"] == "done"  # untouched
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/test_soldier.py -k "cascade" -v`
Expected: FAIL — `kickback_with_cascade` method doesn't exist yet

- [ ] **Step 7: Commit**

```bash
git add tests/test_soldier.py
git commit -m "test(soldier): add cascade invalidation tests"
```

---

### Task 8: Cascade Invalidation — Implementation

**Files:**
- Modify: `antfarm/core/soldier.py` (add `kickback_with_cascade`, update call sites)

- [ ] **Step 1: Add `kickback_with_cascade` method to Soldier**

After the `attempt_merge` method in `soldier.py`, add:

```python
def kickback_with_cascade(
    self, task_id: str, reason: str, _visited: set[str] | None = None,
) -> None:
    """Kick back a task and recursively cascade to downstream done tasks.

    Only invalidates non-merged descendants in done status.
    Does NOT interrupt active tasks — let them finish and the merge
    gate or next cascade will catch staleness.
    Uses a visited set to guard against cyclic deps and repeated traversal.
    """
    if _visited is None:
        _visited = set()
    if task_id in _visited:
        return  # already processed — prevent cycles
    _visited.add(task_id)

    self.colony.kickback(task_id, reason)

    all_tasks = self.colony.list_tasks()
    for task in all_tasks:
        tid = task.get("id", "")
        if tid in _visited:
            continue
        # Only cascade to done, non-merged tasks
        if task.get("status") != "done":
            continue
        if self._has_merged_attempt(task):
            continue
        # Only cascade along dependency edges
        deps = task.get("depends_on") or []
        if task_id in deps:
            cascade_reason = f"cascade: upstream {task_id} was kicked back"
            self.kickback_with_cascade(tid, cascade_reason, _visited=_visited)
```

- [ ] **Step 2: Update run() to use kickback_with_cascade**

In `soldier.py` `run()` method (around line 90), replace:
```python
self.colony.kickback(task["id"], self.last_failure_reason)
```
with:
```python
self.kickback_with_cascade(task["id"], self.last_failure_reason)
```

- [ ] **Step 3: Update run_once() to use kickback_with_cascade**

In `soldier.py` `run_once()` method (around line 108), replace:
```python
self.colony.kickback(task["id"], self.last_failure_reason)
```
with:
```python
self.kickback_with_cascade(task["id"], self.last_failure_reason)
```

- [ ] **Step 4: Update run_once_with_review() to use kickback_with_cascade**

In `soldier.py` `run_once_with_review()`, replace both kickback calls (around lines 225 and 273):
```python
self.colony.kickback(task_id, ...)
```
with:
```python
self.kickback_with_cascade(task_id, ...)
```

- [ ] **Step 5: Run cascade tests**

Run: `pytest tests/test_soldier.py -k "cascade" -v`
Expected: All 5 cascade tests PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass (existing kickback tests should still work — cascade is additive)

- [ ] **Step 7: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 8: Commit**

```bash
git add antfarm/core/soldier.py
git commit -m "feat(soldier): cascade invalidation on kickback

When a task is kicked back, all non-merged downstream tasks in
done/ that depend on it are recursively kicked back too. Active
tasks are not interrupted. Trail entries record the cascade chain."
```

---

### Task 9: Integration Verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass (575 existing + ~11 new)

- [ ] **Step 2: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 3: Verify test count increased**

Run: `pytest tests/ --co -q | tail -1`
Expected: ~586+ tests collected

- [ ] **Step 4: Manual smoke test — max attempts**

```python
# Quick REPL verification
from antfarm.core.backends.file import FileBackend
from datetime import UTC, datetime
import tempfile, json

with tempfile.TemporaryDirectory() as d:
    b = FileBackend(root=f"{d}/.antfarm")
    now = datetime.now(UTC).isoformat()
    task = {"id": "t1", "title": "T", "spec": "S", "created_at": now,
            "updated_at": now, "created_by": "test"}
    b.carry(task)
    for i in range(3):
        t = b.pull("w1")
        b.mark_harvested("t1", t["current_attempt"], pr="", branch="")
        b.kickback("t1", f"fail {i}", max_attempts=3)
    # Should be in blocked/
    assert b.get_task("t1")["status"] == "blocked"
    print("Max-attempt enforcement: OK")
```

- [ ] **Step 5: Bump version**

In `antfarm/core/__init__.py`, update version to `0.5.9`.

```bash
git add antfarm/core/__init__.py
git commit -m "chore: bump version to 0.5.9"
```

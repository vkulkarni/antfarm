# Antfarm v0.6.1 — Implementation Plan

**Status:** DRAFT — awaiting approval
**Derived from:** `docs/SPEC_v06.md` (v0.6.1 section, updated 2026-04-15)
**Prerequisite:** v0.6.0 shipped on `main` as of `debeb28`. 755 tests passing, ruff clean.
**Scope:** Runner, multi-node autoscaler, prompt cache sharing. GitHub Issue Sync deferred to v0.6.2+.
**Goal:** `antfarm runner` on remote machines + Colony autoscaler distributes work across nodes. Prompt cache sharing reduces token cost for parallel builders.

---

## What's in / what's out (v0.6.1)

**IN:**
1. Runner daemon (`antfarm/core/runner.py`) — desired-state reconciliation on remote nodes
2. Extended Node model — `runner_url`, `max_workers`, `capabilities` fields
3. Actuator abstraction — `LocalActuator` (existing subprocess path), `RemoteActuator` (HTTP to Runner)
4. PlacementStrategy — distributes desired worker counts across nodes
5. Multi-node autoscaler — extends existing autoscaler with actuator + placement
6. Colony API extensions — `PUT /nodes/{id}/desired-state`, `GET /nodes/{id}/actual-state`
7. Prompt cache sharing — Queen generates per-mission context blob, Worker prepends to agent prompt
8. CLI: `antfarm runner` command
9. Runner health/liveness in Doctor checks

**OUT (deferred):**
- GitHub Issue Sync (v0.6.2+)
- Unifying single-host autoscaler under actuator abstraction (future cleanup)
- Cross-node task pinning / affinity rules
- Runner authentication (POSIX trusted network, same as v0.1)
- Runner auto-discovery (explicit `runner_url` registration only)

---

## Hard Requirements

These are non-negotiable constraints that cut across phases:

1. **Shared scaling logic is mandatory.** `compute_desired()`, `_count_scope_groups()`, and rate-limit backoff must be extracted into standalone functions (not instance methods) so both `Autoscaler` and `MultiNodeAutoscaler` call the same code. Two copies of scaling logic that can drift is not acceptable. This extraction happens in Phase 5 (PR 6) but is a prerequisite for approval of that PR.

2. **Single source of truth for runner URLs.** `RemoteActuator` does NOT maintain its own `node_id → runner_url` map. It reads `runner_url` from backend Node records on every reconcile cycle. The autoscaler passes full node dicts (including `runner_url`) into actuator methods. No separate registry, no cache that can go stale.

3. **Prompt cache sharing is feature-flagged.** Gated by `enable_mission_context=false` in `QueenConfig` (single owner — Queen generates the blob, workers consume it; no duplicate flag in `AutoscalerConfig`). Off by default until runner/autoscaler are stable in dogfooding. When disabled, workers proceed without context prefix — no error, no codepath difference except the prepend.

4. **PID files for runner restart adoption.** Runner writes PID files to `{state_dir}/pids/{worker_name}.pid` for each managed worker process. On restart, Runner scans PID files, validates processes are alive (`os.kill(pid, 0)`), and adopts them. No generic process scanning. PID files are the only adoption mechanism.

5. **Runner binds to loopback by default.** `--host` defaults to `127.0.0.1`, not `0.0.0.0`. Operators must explicitly pass `--host 0.0.0.0` or a LAN address for multi-node use. Docs and CLI help text must state: "Runner API has no authentication. Do not expose to untrusted networks."

---

## Current codebase anchors (grounding)

| Concept | Real symbol / file |
|---------|-------------------|
| Single-host autoscaler | `Autoscaler` at `antfarm/core/autoscaler.py:50` |
| `_start_worker()` (subprocess) | `autoscaler.py:190` — `subprocess.Popen(cmd, ...)` |
| `_stop_idle_worker()` | `autoscaler.py:240` — checks colony status for idle, then `terminate()` |
| `_compute_desired()` | `autoscaler.py:95` — scope groups, rate-limit backoff |
| `_count_scope_groups()` | `autoscaler.py:156` — union-find by touches |
| `_reconcile()` | `autoscaler.py:86` — cleanup → list tasks/workers → compute → reconcile per role |
| `AutoscalerConfig` | `autoscaler.py:27` — `max_builders`, `max_reviewers`, `poll_interval`, etc. |
| `ManagedWorker` | `autoscaler.py:43` — name, role, worker_id, process |
| Node model | `models.py:527` — `node_id`, `joined_at`, `last_seen` (no runner_url/capabilities yet) |
| Node registration API | `serve.py:384` — `POST /nodes` |
| `ColonyClient.register_node()` | `colony_client.py:32` |
| Queen controller | `queen.py:54` — `_advance_building()` at line 243 |
| Worker runtime | `worker.py:198` — `WorkerRuntime` |
| Worker agent launch | `worker.py:627` — `_launch_agent()` builds prompt string |
| Mission context storage | `.antfarm/missions/` directory (already exists for mission JSON) |
| FileBackend node storage | `.antfarm/nodes/` directory |

---

## Module Map

### New files

```
antfarm/core/
  runner.py              # Runner daemon: desired-state reconciliation, local worker management
  actuator.py            # Actuator ABC, LocalActuator, RemoteActuator
  placement.py           # PlacementStrategy: distribute desired counts across nodes
  mission_context.py     # Prompt cache: generate + store per-mission context blobs

tests/
  test_runner.py         # Runner reconciliation, drain, generation, crash restart
  test_actuator.py       # LocalActuator, RemoteActuator (mocked HTTP)
  test_placement.py      # PlacementStrategy distribution logic
  test_mission_context.py # Context blob generation, worker prepend
```

### Modified files

```
antfarm/core/models.py       # Extend Node dataclass with runner_url, max_workers, capabilities
antfarm/core/autoscaler.py   # Add multi-node path: actuator + placement (single-host unchanged)
antfarm/core/serve.py        # New endpoints: desired-state push, actual-state read, runner registration
antfarm/core/colony_client.py # Client methods for new endpoints: get_mission_context(mission_id), plus node/runner state endpoints
antfarm/core/queen.py        # Generate mission_context blob on build phase entry
antfarm/core/worker.py       # Prepend mission_context to agent prompt
antfarm/core/cli.py          # Add `antfarm runner` command
antfarm/core/doctor.py       # Add runner health checks (unreachable, stale)
antfarm/core/backends/base.py # list_nodes() method (if not already present)
antfarm/core/backends/file.py # list_nodes() implementation
```

---

## Build Phases

### Phase 1: Extend Node Model

**Goal:** Node has the fields needed for multi-node operation.

#### `models.py` changes

```python
@dataclass
class Node:
    node_id: str
    joined_at: str
    last_seen: str
    runner_url: str | None = None          # "http://192.168.1.10:7434"
    max_workers: int = 4
    capabilities: list[str] = field(default_factory=list)  # ["gpu", "docker"]
```

Update `to_dict()` / `from_dict()` to include new fields with backward-compatible defaults.

#### `serve.py` changes

Extend `NodeRequest` pydantic model:

```python
class NodeRequest(BaseModel):
    node_id: str
    runner_url: str | None = None
    max_workers: int = 4
    capabilities: list[str] = Field(default_factory=list)
```

Add `GET /nodes` endpoint to list registered nodes. Add `GET /nodes/{id}` for single node detail.

#### `backends/file.py` changes

Add `list_nodes()` → reads all files from `.antfarm/nodes/`. Update `register_node()` to persist new fields.

**Tests:**
1. `test_node_roundtrip` — Node with runner_url/max_workers/capabilities serializes and deserializes
2. `test_node_backward_compat` — Node JSON without new fields loads with defaults
3. `test_register_node_with_runner_url` — registration persists runner_url
4. `test_list_nodes` — returns all registered nodes

---

### Phase 2: Runner Daemon

**Goal:** `antfarm runner` runs on a remote machine, reconciles local workers to desired state.

#### `runner.py`

```python
@dataclass
class DesiredState:
    generation: int
    desired: dict[str, int]       # {"builder": 2, "reviewer": 1, "planner": 0}
    drain: list[str] = field(default_factory=list)  # reserved for future per-worker drain
    # v0.6.1 downscaling: Colony lowers desired counts, Runner only stops idle
    # workers. Explicit per-worker drain lists (e.g. drain: ["builder-4"]) are
    # reserved for v0.6.2+. For now, drain is always [].
    #
    # NOTE: agent_type, repo_path, workspace_root, colony_url, token are all
    # runner-local bootstrap config set at startup. They are NOT sent in
    # desired-state updates. This keeps DesiredState minimal and avoids a
    # second config channel.


@dataclass
class ActualState:
    applied_generation: int
    workers: dict[str, dict]      # name → {"role": "builder", "pid": 123, "status": "running"}
    capacity: dict                # {"cpus": 8, "max_workers": 4, "available": 2}


class Runner:
    """Desired-state worker reconciler for remote nodes.

    Receives target state from Colony, reconciles local worker processes
    to match. Self-heals crashed processes. Reports actual state back.
    """

    def __init__(
        self,
        node_id: str,
        colony_url: str,
        repo_path: str,
        workspace_root: str,
        integration_branch: str = "main",
        max_workers: int = 4,
        capabilities: list[str] | None = None,
        port: int = 7434,
        agent_type: str = "claude-code",
        token: str | None = None,
        reconcile_interval: float = 15.0,
        fetch_interval: float = 300.0,
    ):
        ...

    def run(self) -> None:
        """Main loop:
        1. Register node with colony (including runner_url)
        2. Start local HTTP API (FastAPI/uvicorn)
        3. Start reconciliation loop (background thread)
        4. Start git fetch loop (background thread)
        """
        ...

    def reconcile(self) -> None:
        """Compare desired vs actual, start/stop workers to converge."""
        ...

    def apply_desired_state(self, state: DesiredState) -> None:
        """Receive new desired state from colony. Only apply if generation >= current."""
        ...

    def get_actual_state(self) -> ActualState:
        """Report current worker processes, capacity, applied generation."""
        ...

    def _start_worker(self, role: str) -> None:
        """Spawn a local worker subprocess (same as autoscaler._start_worker)."""
        ...

    def _stop_idle_worker(self, role: str) -> bool:
        """Stop one idle worker. Returns True if stopped."""
        ...

    def _restart_crashed(self) -> None:
        """Detect exited processes, restart up to desired count.
        Uses PID files in {state_dir}/pids/ — see Hard Requirement #4."""
        ...

    def _write_pid_file(self, worker_name: str, pid: int) -> None:
        """Write PID file to {state_dir}/pids/{worker_name}.pid."""
        ...

    def _remove_pid_file(self, worker_name: str) -> None:
        """Remove PID file on clean worker shutdown."""
        ...

    def _adopt_existing_workers(self) -> None:
        """On startup: scan {state_dir}/pids/, validate PIDs via os.kill(pid, 0),
        adopt live processes into managed set. Remove stale PID files.
        Starts with applied_generation=0 (colony will push fresh state)."""
        ...

    def _git_fetch_loop(self) -> None:
        """Periodic git fetch origin in repo_path."""
        ...
```

**Runner HTTP API (local FastAPI app on port 7434):**

```python
@app.put("/desired-state")
def put_desired_state(state: DesiredStateRequest): ...

@app.get("/actual-state")
def get_actual_state() -> ActualStateResponse: ...

@app.get("/capacity")
def get_capacity() -> CapacityResponse: ...

@app.get("/health")
def get_health() -> dict: ...
```

**Key behaviors:**
- Generation monotonic: reject desired state with generation < applied_generation
- Self-healing: reconciliation loop restarts crashed workers without waiting for Colony
- Drain: workers in `drain` list finish current task, then stop (check colony for idle status)
- Git fetch: periodic `git fetch origin` (default every 5 minutes) so worktrees start from recent state
- PID files: every started worker writes `{state_dir}/pids/{worker_name}.pid`. On restart, Runner adopts live processes from PID files (Hard Requirement #4)

**Security boundary (Hard Requirement #5):**
- Runner binds to `127.0.0.1` by default (`--host` flag to override)
- Runner API has NO authentication in v0.6.1 (trusted private network only)
- CLI help and docs must state: "Do not expose to untrusted networks"
- For multi-node use, operator explicitly passes `--host 0.0.0.0` or a LAN address

**Tests:**
1. `test_apply_desired_state` — sets desired, reconcile starts workers
2. `test_generation_monotonic` — older generation rejected
3. `test_reconcile_starts_missing` — desired 2 builders, 0 running → starts 2
4. `test_reconcile_stops_excess` — desired 1 builder, 3 running (2 idle) → stops 2 idle
5. `test_drain_finishes_active` — drained worker with active task keeps running until idle
6. `test_restart_crashed` — crashed process detected and restarted
7. `test_actual_state_reports_correctly` — actual state reflects running processes + generation
8. `test_health_endpoint` — returns ok when runner is alive
9. `test_pid_file_written` — starting a worker creates PID file
10. `test_adopt_existing_on_restart` — Runner startup adopts live processes from PID files
11. `test_stale_pid_cleaned` — PID file for dead process is removed on startup
12. `test_default_bind_loopback` — Runner binds to 127.0.0.1 by default

---

### Phase 3: Actuator Abstraction

**Goal:** Pluggable execution backend for the autoscaler.

#### `actuator.py`

```python
from abc import ABC, abstractmethod


class Actuator(ABC):
    """Interface for applying desired worker state to a node.

    Methods receive runner_url directly — the autoscaler reads URLs from
    backend Node records (single source of truth) and passes them through.
    Actuators do not maintain their own URL registries.
    """

    @abstractmethod
    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        """Push desired worker counts to a node."""
        ...

    @abstractmethod
    def get_actual(self, runner_url: str) -> dict:
        """Get actual worker state from a node. Returns {"workers": {...}, "applied_generation": N}."""
        ...

    @abstractmethod
    def is_reachable(self, runner_url: str) -> bool:
        """Check if the node is reachable."""
        ...


class LocalActuator(Actuator):
    """Wraps existing subprocess-based worker management for the local node.

    This is a thin adapter over the existing Autoscaler._start_worker / _stop_idle_worker
    logic. It keeps the v0.6.0 single-host behavior working through the actuator interface
    WITHOUT changing the existing Autoscaler class.
    """

    def __init__(self, autoscaler: "Autoscaler"):
        self._autoscaler = autoscaler

    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        """Reconcile local processes to match desired counts. runner_url ignored (local)."""
        actual = self._autoscaler._count_actual()
        for role in ("planner", "builder", "reviewer"):
            self._autoscaler._reconcile_role(role, desired.get(role, 0), actual.get(role, 0))

    def get_actual(self, runner_url: str) -> dict:
        return {
            "workers": {
                name: {"role": mw.role, "pid": mw.process.pid}
                for name, mw in self._autoscaler.managed.items()
                if mw.process.poll() is None
            },
            "applied_generation": -1,  # local doesn't use generations
        }

    def is_reachable(self, runner_url: str) -> bool:
        return True  # always reachable


class RemoteActuator(Actuator):
    """Pushes desired state to a remote Runner via HTTP.

    Does NOT maintain its own runner_url registry. All methods receive
    runner_url via the node dict passed by the autoscaler. The backend
    Node records are the single source of truth for runner URLs.
    """

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        import httpx
        httpx.put(
            f"{runner_url}/desired-state",
            json={"generation": generation, "desired": desired, "drain": []},
            timeout=self._timeout,
        )

    def get_actual(self, runner_url: str) -> dict:
        import httpx
        r = httpx.get(f"{runner_url}/actual-state", timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def is_reachable(self, runner_url: str) -> bool:
        if not runner_url:
            return False
        import httpx
        try:
            r = httpx.get(f"{runner_url}/health", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False
```

**Tests:**
1. `test_local_actuator_apply` — calls reconcile_role with correct desired counts
2. `test_local_actuator_get_actual` — returns running processes
3. `test_remote_actuator_apply` — sends PUT /desired-state with correct JSON
4. `test_remote_actuator_get_actual` — reads GET /actual-state
5. `test_remote_actuator_unreachable` — is_reachable returns False on timeout

---

### Phase 4: Placement Strategy

**Goal:** Distribute desired worker counts across nodes.

#### `placement.py`

```python
@dataclass
class NodeCapacity:
    node_id: str
    max_workers: int
    current_workers: int
    capabilities: list[str]
    reachable: bool


def compute_placement(
    desired_total: dict[str, int],
    nodes: list[NodeCapacity],
) -> dict[str, dict[str, int]]:
    """Distribute desired worker counts across nodes.

    Args:
        desired_total: Global desired counts, e.g. {"builder": 4, "reviewer": 2, "planner": 1}
        nodes: Available nodes with capacity info.

    Returns:
        Per-node desired counts, e.g. {"node-1": {"builder": 2, "reviewer": 1}, "node-2": {"builder": 2, "reviewer": 1, "planner": 1}}

    Strategy:
    1. Filter to reachable nodes only.
    2. For each role, distribute round-robin across nodes with available capacity.
    3. Respect per-node max_workers cap.
    4. Prefer nodes with matching capabilities for specialized roles (future).
    """
    ...
```

**Distribution rules:**
- Round-robin across reachable nodes with available capacity slots
- Respect `max_workers` per node (total across all roles)
- If total desired > total capacity, fill what we can (no error, just under-provision)
- Deterministic output for same inputs (sorted node order)

**Tests:**
1. `test_single_node_gets_all` — one node, all workers assigned to it
2. `test_round_robin_distribution` — 4 builders across 2 nodes → 2 each
3. `test_respects_max_workers` — node with max_workers=2 gets at most 2
4. `test_unreachable_node_skipped` — unreachable node gets 0 workers
5. `test_over_capacity` — desired 10 but total capacity 6 → places 6
6. `test_deterministic` — same inputs produce same output

---

### Phase 5: Multi-Node Autoscaler

**Goal:** Extend existing autoscaler to use actuators and placement for multi-node operation.

#### `autoscaler.py` changes

Add to `AutoscalerConfig`:
```python
@dataclass
class AutoscalerConfig:
    # ... existing fields ...
    multi_node: bool = False          # enable multi-node mode
    generation_counter: int = 0       # monotonic generation for desired state
```

Add a new `MultiNodeAutoscaler` class (or extend `Autoscaler` with a multi-node reconcile path):

```python
class MultiNodeAutoscaler:
    """Multi-node autoscaler using actuators and placement.

    Extends the scaling logic from Autoscaler (compute_desired, scope groups, rate-limit)
    but distributes across nodes via PlacementStrategy and RemoteActuator.

    The existing single-host Autoscaler is NOT modified. This is a separate class
    that reuses the computation helpers.
    """

    def __init__(
        self,
        backend: TaskBackend,
        config: AutoscalerConfig,
        actuator: RemoteActuator,
        clock=time.time,
    ):
        self.backend = backend
        self.config = config
        self.actuator = actuator
        self._clock = clock
        self._generation = 0
        self._stopped = False
        self._node_urls: dict[str, str] = {}  # refreshed from backend each reconcile cycle

    def run(self) -> None:
        """Main loop: compute desired → place → push to runners."""
        while not self._stopped:
            try:
                self._reconcile()
            except Exception as e:
                logger.exception("multi-node autoscaler reconcile failed: %s", e)
            time.sleep(self.config.poll_interval)

    def _reconcile(self) -> None:
        tasks = self.backend.list_tasks()
        workers = self.backend.list_workers()
        nodes = self._get_node_capacities()

        # Shared scaling logic (extracted standalone function — see Hard Requirements #1)
        desired_total = compute_desired(tasks, workers, self.config)

        # Distribute across nodes
        placement = compute_placement(desired_total, nodes)

        # Push to each node via runner_url (read from backend — see Hard Requirements #2)
        self._generation += 1
        for node_id, desired in placement.items():
            runner_url = self._node_urls.get(node_id)
            if not runner_url:
                continue
            try:
                self.actuator.apply(runner_url, desired, self._generation)
            except Exception as e:
                logger.warning("failed to push desired state to %s: %s", node_id, e)

    def _get_node_capacities(self) -> list[NodeCapacity]:
        """Read nodes from backend, check reachability via actuator.

        runner_url is read from backend Node records — single source of truth.
        """
        nodes = self.backend.list_nodes()
        self._node_urls = {}  # refreshed every cycle from backend
        capacities = []
        for n in nodes:
            runner_url = n.get("runner_url")
            if not runner_url:
                continue  # skip nodes without a runner
            self._node_urls[n["node_id"]] = runner_url
            reachable = self.actuator.is_reachable(runner_url)
            current = 0
            if reachable:
                try:
                    actual = self.actuator.get_actual(runner_url)
                    current = len(actual.get("workers", {}))
                except Exception:
                    reachable = False
            capacities.append(NodeCapacity(
                node_id=n["node_id"],
                max_workers=n.get("max_workers", 4),
                current_workers=current,
                capabilities=n.get("capabilities", []),
                reachable=reachable,
            ))
        return capacities
```

**MANDATORY refactoring (Hard Requirement #1):** Extract `_compute_desired()`, `_count_scope_groups()`, `_is_rate_limited()`, `_has_verdict()`, and `_has_merged_attempt()` from `Autoscaler` into standalone module-level functions (e.g. `compute_desired(tasks, workers, config)`, `count_scope_groups(tasks)`). Both `Autoscaler` and `MultiNodeAutoscaler` must call these shared functions. The existing `Autoscaler` class updates its `_reconcile()` to call the extracted functions — this is a refactor, not a behavior change, and must be covered by existing tests continuing to pass.

#### `serve.py` changes

Colony startup logic:
- If `--multi-node` flag: start `MultiNodeAutoscaler` thread instead of `Autoscaler`
- Otherwise: existing single-host `Autoscaler` behavior (unchanged)

New endpoints for Runner communication:
```python
@app.put("/nodes/{node_id}/desired-state")
def push_desired_state(node_id: str, state: DesiredStateRequest): ...

@app.get("/nodes/{node_id}/actual-state")
def get_actual_state(node_id: str): ...
```

These are **Colony-side endpoints** that proxy to the Runner. The autoscaler pushes state to Runners directly, but these endpoints allow CLI/TUI to inspect state.

**Tests:**
1. `test_multi_node_reconcile` — computes desired, places across 2 nodes, pushes to actuator
2. `test_generation_increments` — each reconcile bumps generation
3. `test_unreachable_node_skipped` — node with failed health check gets no workers
4. `test_single_host_behavior_unchanged` — existing Autoscaler tests pass after shared-logic extraction refactor
5. `test_compute_desired_extracted` — extracted standalone function matches existing Autoscaler behavior (all existing autoscaler tests still pass after refactor)

---

### Phase 6: Prompt Cache Sharing

**Goal:** Reduce token cost for parallel builders in the same mission.

**Feature-flagged (Hard Requirement #3):** Gated by `enable_mission_context=False` in `QueenConfig` (single owner — not duplicated in `AutoscalerConfig`). Off by default until runner/autoscaler are stable. When disabled, Queen skips context generation and workers proceed without prefix. No error, no codepath difference except the prepend is skipped.

#### `mission_context.py`

```python
def generate_mission_context(
    repo_path: str,
    integration_branch: str,
    mission: dict,
    plan_artifact: dict | None = None,
) -> str:
    """Generate a shared context prefix for all builders in a mission.

    Collects:
    1. Project conventions (CLAUDE.md, AGENTS.md if present)
    2. Integration branch state (recent commits, changed files)
    3. Mission spec summary
    4. Plan artifact summary (task list, dependency graph)
    5. Key file listings (src structure)

    Returns a markdown string that will be prepended identically to every
    builder's prompt. Must be byte-identical across builders for cache sharing
    to work.

    The content is deterministic for the same inputs — no timestamps, no
    random values, no machine-specific paths.
    """
    ...


def store_mission_context(data_dir: str, mission_id: str, context: str) -> str:
    """Write context blob to .antfarm/missions/{mission_id}_context.md.

    Returns the file path.
    """
    ...


def load_mission_context(data_dir: str, mission_id: str) -> str | None:
    """Load stored context blob. Returns None if not found."""
    ...
```

#### `queen.py` changes

When a mission transitions to `building` phase (after plan is approved), generate the context blob:

```python
def _advance_reviewing_plan(self, mission: dict) -> None:
    # ... existing plan approval logic ...
    # After carrying child tasks:
    from antfarm.core.mission_context import generate_mission_context, store_mission_context
    context = generate_mission_context(
        repo_path=self._repo_path,
        integration_branch=self._integration_branch,
        mission=mission,
        plan_artifact=mission.get("plan_artifact"),
    )
    store_mission_context(self._data_dir, mission["mission_id"], context)
```

#### `worker.py` changes

In `_launch_agent()`, for builder tasks with a `mission_id`, prepend the mission context:

```python
def _launch_agent(self, task, workspace) -> AgentResult:
    # ... existing prompt building ...

    # Prepend mission context for cache sharing
    if task.get("mission_id") and not is_plan and not is_review:
        from antfarm.core.mission_context import get_mission_context
        context = get_mission_context(
            mission_id=task["mission_id"],
            data_dir=self._data_dir,
            colony_client=self.colony,
        )
        if context:
            prompt = context + "\n\n---\n\n" + prompt
    # ... rest of launch ...
```

**Context fetch abstraction (`mission_context.py`):**

```python
def get_mission_context(
    mission_id: str,
    data_dir: str | None = None,
    colony_client: ColonyClient | None = None,
) -> str | None:
    """Load mission context, abstracting local vs remote.

    - Local node: reads from {data_dir}/missions/{mission_id}_context.md
    - Remote node: fetches via colony_client.get_mission_context(mission_id)
    - Falls back to colony_client if local file not found (remote worker)
    - Returns None if unavailable (graceful degradation)
    """
    ...
```

Both paths must return byte-identical content for cache sharing to work. The Colony API serves the same blob that was written locally by the Queen.

**Important for cache sharing:** The context prefix must be byte-identical across all builders in the same mission. This means:
- No timestamps or random values in the context
- No machine-specific absolute paths
- Local and remote fetch return the same blob (Colony serves what Queen wrote)

#### Colony API for context distribution

Add endpoint for remote runners to fetch mission context:
```python
@app.get("/missions/{mission_id}/context")
def get_mission_context(mission_id: str): ...
```

Workers on remote nodes fetch context from Colony rather than reading local `.antfarm/` directory.

**Tests:**
1. `test_generate_context_deterministic` — same inputs produce byte-identical output
2. `test_store_and_load_context` — roundtrip through filesystem
3. `test_context_no_timestamps` — output contains no ISO timestamps or epoch values
4. `test_worker_prepends_context` — builder prompt starts with mission context
5. `test_worker_skips_context_for_planner` — planner tasks don't get context prefix
6. `test_worker_skips_context_for_reviewer` — reviewer tasks don't get context prefix
7. `test_context_api_endpoint` — GET /missions/{id}/context returns stored blob
8. `test_context_disabled_by_default` — Queen skips context generation when enable_mission_context=False
9. `test_worker_no_prefix_when_disabled` — worker proceeds without context when flag is off

---

### Phase 7: CLI + Doctor

**Goal:** `antfarm runner` CLI command and health checks.

#### `cli.py` changes

```python
@main.command()
@click.option("--colony-url", required=True)
@click.option("--repo-path", required=True)
@click.option("--workspace-root", default=None)
@click.option("--node", default=None, help="Node ID (default: hostname)")
@click.option("--host", default="127.0.0.1", help="Bind address. Default: loopback only. Use 0.0.0.0 for multi-node. WARNING: no auth — do not expose to untrusted networks.")
@click.option("--port", default=7434)
@click.option("--max-workers", default=4, type=int)
@click.option("--agent", default="claude-code")
@click.option("--integration-branch", default="main")
@click.option("--capabilities", default="", help="Comma-separated: gpu,docker")
@click.option("--token", default=None)
def runner(colony_url, repo_path, workspace_root, node, host, port, max_workers, agent,
           integration_branch, capabilities, token):
    """Start a Runner daemon on this machine.

    The Runner API has no authentication. Bind to loopback (default) or a
    private LAN address only. Do not expose to untrusted networks.
    """
    ...
```

Add `--multi-node` flag to `antfarm colony`:
```python
@main.command()
@click.option("--multi-node", is_flag=True, default=False, help="Enable multi-node autoscaler")
def colony(..., multi_node): ...
```

#### `doctor.py` changes

New checks:
- `check_runner_reachable` — for each node with a runner_url, check GET /health
- `check_runner_state_drift` — compare colony's desired state with runner's applied_generation
- `check_stale_remote_workers` — workers on remote nodes whose runner is unreachable

**Tests:**
1. `test_cli_runner_starts` — runner command starts daemon (mock subprocess)
2. `test_cli_colony_multi_node` — --multi-node flag starts MultiNodeAutoscaler
3. `test_doctor_runner_unreachable` — unreachable runner flagged
4. `test_doctor_state_drift` — stale applied_generation flagged

---

### Phase 8: End-to-End Test

**Goal:** Full multi-node loop works in a single test.

```python
def test_e2e_multi_node(tmp_path):
    """
    1. Start colony with multi-node autoscaler (in-process)
    2. Start 2 runner instances (in-process, different ports)
    3. Register both nodes with runner_urls
    4. Create a mission with 4 tasks
    5. Colony autoscaler computes desired state, distributes across nodes
    6. Runners receive desired state, start worker subprocesses (mocked)
    7. Workers forage, execute (mocked agent), harvest
    8. Soldier merges (mocked git)
    9. Verify: tasks distributed across both nodes
    10. Verify: mission completes
    11. Doctor finds no issues
    """
```

```python
def test_e2e_prompt_cache(tmp_path):
    """
    1. Start colony (in-process)
    2. Create mission, plan produces 3 tasks
    3. Queen generates mission context blob
    4. Worker launches builders — verify all 3 get identical context prefix
    5. Verify context blob is deterministic (generate twice, compare)
    """
```

---

## PR Sequence

| # | PR | Scope | Depends on |
|---|-----|-------|------------|
| 1 | `feat(models): extend Node with runner_url, max_workers, capabilities` | Phase 1 | — |
| 2 | `feat(backend): add list_nodes + node field persistence` | Phase 1 | PR 1 |
| 3 | `feat(runner): Runner daemon with desired-state reconciliation` | Phase 2 | PR 2 |
| 4 | `feat(autoscaler): actuator abstraction (Local + Remote)` | Phase 3 | PR 2 |
| 5 | `feat(autoscaler): placement strategy` | Phase 4 | PR 4 |
| 6 | `feat(autoscaler): multi-node autoscaler` | Phase 5 | PR 3, 4, 5 |
| 7 | `feat(worker): prompt cache sharing — context generation + prepend` | Phase 6 | PR 2 |
| 8 | `feat(cli): antfarm runner command + colony --multi-node` | Phase 7 | PR 3, 6 |
| 9 | `feat(doctor): runner health checks` | Phase 7 | PR 3 |
| 10 | `test(e2e): multi-node + prompt cache end-to-end tests` | Phase 8 | PR 6, 7 |

PRs 1-2 are foundation. PRs 3-5 can be developed in parallel after PR 2. PR 6 integrates them. PR 7 (prompt cache) is independent of PRs 3-6 and can be developed in parallel.

---

## State Machines

### Runner Desired-State Flow

```
Colony Autoscaler          Runner
     │                       │
     │  _reconcile()         │
     │  ├─ compute_desired() │
     │  ├─ compute_placement()│
     │  └─ actuator.apply()  │
     │        │               │
     │  PUT /desired-state ──►│
     │  {generation: 17,      │
     │   desired: {builder:2}}│
     │                        │
     │                   reconcile()
     │                   ├─ compare desired vs actual
     │                   ├─ _start_worker() if missing
     │                   ├─ _stop_idle_worker() if excess
     │                   └─ _restart_crashed() if dead
     │                        │
     │  ◄── GET /actual-state │
     │  {applied_generation:17│
     │   workers: {...}}      │
```

### Generation Protocol

```
Colony sends generation 15 ──► Runner applies, reports applied=15
Colony sends generation 16 ──► Runner applies, reports applied=16
  (network partition)
Colony sends generation 17 ──► lost
Colony sends generation 18 ──► Runner applies (skips 17), reports applied=18
Colony sends generation 16 ──► Runner REJECTS (16 < 18)
```

---

## Edge Cases and Invariants

### Runner Invariants

| Rule | Details |
|------|---------|
| **Generation monotonic** | Runner rejects desired state with generation < applied_generation |
| **Drain = idle only** | Never kill a worker with an active task. Wait for harvest, then stop. |
| **Self-healing** | Crashed worker processes are restarted in next reconcile loop (up to desired count) |
| **Colony unreachable** | Runner keeps current workers running. No scale-up or scale-down until reconnect. |
| **Runner restart (PID adoption)** | On startup, Runner scans `{state_dir}/pids/*.pid` files, validates each PID via `os.kill(pid, 0)`, and adopts live processes. Stale PID files (process dead) are removed. No generic process scanning. Starts with `applied_generation=0` — Colony pushes fresh desired state on next cycle. |

### Autoscaler Invariants

| Rule | Details |
|------|---------|
| **Single-host behavior unchanged** | Existing `Autoscaler` scaling behavior is preserved. The class is refactored to call shared extracted functions (Hard Requirement #1), but observable behavior and test outcomes do not change. |
| **Reachability check before push** | Don't push desired state to unreachable runners |
| **Generation never decrements** | Colony generation counter is monotonically increasing |
| **Under-provision is safe** | If total capacity < desired, place what fits. No error. |

### Prompt Cache Invariants

| Rule | Details |
|------|---------|
| **Byte-identical** | Same mission + same repo state = identical context blob |
| **No ephemeral data** | No timestamps, no random values, no PIDs in context |
| **Generated once** | Queen generates blob once when entering build phase. Not regenerated per-builder. |
| **Graceful degradation** | Missing context blob = worker proceeds without prefix. No error. |
| **Remote fetch** | Workers on remote nodes fetch context from Colony API, not local filesystem |

---

## Dependencies

No new dependencies beyond what v0.6.0 already uses (FastAPI, uvicorn, httpx, click).

The Runner uses FastAPI/uvicorn for its local HTTP API — same stack as the Colony server.

---

## First 10 Tests

| # | Test | Module | What it proves |
|---|------|--------|----------------|
| 1 | `test_node_extended_roundtrip` | models | Node with runner_url/capabilities serializes correctly |
| 2 | `test_runner_reconcile_starts_workers` | runner | Desired 2, actual 0 → starts 2 workers |
| 3 | `test_runner_generation_monotonic` | runner | Older generation rejected |
| 4 | `test_runner_drain_respects_active` | runner | Active worker not stopped during drain |
| 5 | `test_runner_restart_crashed` | runner | Crashed process restarted |
| 6 | `test_remote_actuator_pushes_state` | actuator | PUT /desired-state sent correctly |
| 7 | `test_placement_round_robin` | placement | 4 builders across 2 nodes = 2 each |
| 8 | `test_placement_respects_capacity` | placement | max_workers cap enforced |
| 9 | `test_mission_context_deterministic` | mission_context | Same inputs = byte-identical output |
| 10 | `test_worker_prepends_context` | worker | Builder prompt starts with mission context |

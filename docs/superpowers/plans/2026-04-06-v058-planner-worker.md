# v0.5.8 — Planner Worker Implementation Plan

**Status:** DRAFT v2 — revised per ChatGPT principal-engineer review
**Derived from:** SPEC_v05.md v0.5.8 section
**Goal:** Remove the human from the front of the pipeline. Operator carries a plan task, planner worker decomposes it into sub-tasks, builders build, reviewers review, Soldier merges. Spec in, merged code out.

---

## Scope

- Planner worker type (`--type planner`, `capabilities=["plan"]`)
- Plan task creation (`antfarm carry --type plan`)
- Planner mode in worker runtime (forage plan task → agent decomposes → validate → carry children)
- Deterministic child IDs (`task-{slug}-01`, `task-{slug}-02` — NOT plan-prefixed)
- Lineage metadata (`spawned_by` on child tasks)
- Guardrails (max children, no recursive plans, acyclic deps)
- Shared PlannerEngine validation (reuse, not duplicate)
- Plan harvest artifact (created IDs, warnings, dep summary)
- TUI badge for plan tasks
- Scheduler restriction (planner workers only forage plan tasks)

## Out of scope

- Tester worker (v0.5.9)
- New TUI "Planning" panel (plan tasks show in Building)
- GitHub issue auto-import (operator copies issue body into spec)
- `task_type` model field (use capabilities + naming convention)

---

## Implementation Order

1. CLI: `--type plan` on carry + `planner` worker type
2. Scheduler: planner workers only forage plan tasks
3. Worker: planner mode in `_launch_agent()` and `_process_one_task()`
4. PlannerEngine: refactor validation into reusable functions
5. Lineage: `spawned_by` field on carried child tasks
6. TUI: plan task badge
7. Tests
8. E2E test

---

## 1. CLI Changes

### File: `antfarm/core/cli.py`

#### carry command — add `--type` option

Add `--type` option alongside existing `--id`:

```python
@click.option("--type", "task_type", default=None,
              type=click.Choice(["plan"]),
              help="Task type: 'plan' for planner decomposition.")
```

When `--type plan`:
- Auto-prefix ID with `plan-` if not already prefixed
- Add `capabilities_required=["plan"]` to payload
- Validate that `--spec` or `--file` is provided (plan tasks need a spec)

```python
if task_type == "plan":
    if not task_id.startswith("plan-"):
        task_id = f"plan-{task_id}"
    payload.setdefault("capabilities_required", [])
    if "plan" not in payload["capabilities_required"]:
        payload["capabilities_required"].append("plan")
```

#### worker start — add `planner` to `--type` choices

Update the `--type` option:

```python
@click.option("--type", "worker_type", default="builder",
              type=click.Choice(["builder", "reviewer", "planner"]),
              help="Worker type: builder (default), reviewer, or planner.")
```

When `planner`:
- Add `"plan"` to capabilities
- Worker name defaults to `"planner"` if `--name` not set

```python
if worker_type == "planner" and "plan" not in caps:
    caps.append("plan")
```

---

## 2. Scheduler Changes

### File: `antfarm/core/scheduler.py`

The existing specialized worker logic already handles this. Workers with `capabilities=["plan"]` are "specialized" and will only forage tasks with `capabilities_required` containing `"plan"`. The existing code at line 50-63:

```python
specialized = worker_capabilities - {"builder"}
if specialized:
    eligible = [
        t for t in eligible
        if set(t.capabilities_required) & specialized
    ]
```

`"plan"` is not `"builder"`, so it's treated as specialized. Planner workers will only forage tasks requiring `"plan"` capability. **No code change needed** — the existing pattern works.

Verify with a test that planner workers don't forage implementation tasks.

---

## 3. Worker: Planner Mode

### File: `antfarm/core/worker.py`

#### 3a. Detect plan task in `_process_one_task()`

After forage, detect task type from `capabilities_required`, NOT string prefix:

```python
task_id = task["id"]
caps_req = set(task.get("capabilities_required", []))
is_plan = "plan" in caps_req
is_review = "review" in caps_req
```

**Why not prefix?** Child tasks use `task-{slug}-01` IDs which would collide with `plan-` prefix detection. Capabilities are the authoritative source of task type.

#### 3b. Planner prompt in `_launch_agent()`

When `is_plan`, build a planning-specific prompt:

```python
if is_plan:
    prompt = (
        f"Task: {title}\n\n"
        f"You are a PLANNER. Decompose this spec into implementation tasks.\n\n"
        f"Spec:\n{spec}\n\n"
        f"You are working in: {workspace}\n"
        f"Read the codebase to understand the project structure.\n\n"
        f"{repo_facts_context}"
        "Output a JSON array of tasks between [PLAN_RESULT] tags.\n"
        "Each task object must have:\n"
        '  - "title": short imperative title\n'
        '  - "spec": detailed implementation instructions (2-5 sentences)\n'
        '  - "touches": list of scope tags (e.g. ["api", "auth"])\n'
        '  - "depends_on": list of task indices (1-based) or []\n'
        '  - "priority": integer 1-20 (lower = higher priority)\n'
        '  - "complexity": "S", "M", or "L"\n\n'
        "Rules:\n"
        "- Maximum 10 tasks\n"
        "- Make tasks as parallel as possible\n"
        "- Use depends_on only when strictly necessary\n"
        "- Each task should be independently implementable\n\n"
        "Example output:\n"
        "[PLAN_RESULT]\n"
        '[{"title": "Add auth middleware", "spec": "...", '
        '"touches": ["api"], "depends_on": [], '
        '"priority": 5, "complexity": "M"}]\n'
        "[/PLAN_RESULT]\n"
    )
```

#### 3c. Plan task processing in `_process_one_task()`

After agent completes successfully on a plan task:

```python
if is_plan and result.returncode == 0:
    plan_result = self._process_plan_output(
        task, attempt_id, result.stdout + result.stderr
    )
    if plan_result:
        # Harvest plan task with artifact
        artifact = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "worker_id": self.worker_id,
            "plan_task_id": task_id,
            "created_task_ids": plan_result["created_ids"],
            "task_count": len(plan_result["created_ids"]),
            "warnings": plan_result["warnings"],
            "dependency_summary": plan_result["dep_summary"],
            "branch": "",
            "pr_url": None,
            "head_commit_sha": "",
            "base_commit_sha": "",
            "target_branch": "",
            "target_branch_sha_at_harvest": "",
            "files_changed": [],
            "lines_added": 0,
            "lines_removed": 0,
        }
        with contextlib.suppress(Exception):
            self.colony.harvest(
                task_id, attempt_id, pr="", branch="",
                artifact=artifact,
            )
        with contextlib.suppress(Exception):
            self.colony.trail(
                task_id, self.worker_id,
                f"plan complete: created {len(plan_result['created_ids'])} tasks",
            )
        return True

    # Plan parsing failed — trail the error
    with contextlib.suppress(Exception):
        self.colony.trail(
            task_id, self.worker_id,
            "plan failed: could not parse agent output into tasks",
        )
    return True
```

#### 3d. Add `_process_plan_output()` method

```python
def _process_plan_output(
    self, task: dict, attempt_id: str, output: str,
) -> dict | None:
    """Parse plan output, validate, and carry child tasks.

    Returns dict with created_ids, warnings, dep_summary on success.
    Returns None if parsing or validation fails.
    """
    import re

    # Extract JSON from [PLAN_RESULT]...[/PLAN_RESULT] tags
    match = re.search(
        r"\[PLAN_RESULT\]\s*(.*?)\s*\[/PLAN_RESULT\]",
        output, re.DOTALL,
    )
    if not match:
        logger.warning("no [PLAN_RESULT] tags in planner output")
        return None

    # Parse and validate using shared PlannerEngine logic
    from antfarm.core.planner import PlannerEngine

    engine = PlannerEngine()
    try:
        proposed = engine.parse_structured_plan(match.group(1))
    except (ValueError, _json.JSONDecodeError) as exc:
        logger.warning("plan parse error: %s", exc)
        return None

    result = engine.validate_plan(proposed)
    if result.errors:
        for err in result.errors:
            logger.warning("plan validation error: %s", err)
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task["id"], self.worker_id,
                    f"plan validation error: {err}",
                )
        return None

    tasks = result.tasks

    # Guardrail: max 10 children
    if len(tasks) > 10:
        logger.warning("plan has %d tasks, max 10", len(tasks))
        with contextlib.suppress(Exception):
            self.colony.trail(
                task["id"], self.worker_id,
                f"plan rejected: {len(tasks)} tasks exceeds max 10",
            )
        return None

    # Generate deterministic child IDs (NOT plan-prefixed — these are impl tasks)
    # Strip "plan-" prefix from parent to get the slug
    parent_id = task["id"]
    slug = parent_id.removeprefix("plan-")
    child_ids = [f"task-{slug}-{i:02d}" for i in range(1, len(tasks) + 1)]

    # Resolve index-based deps to child IDs
    from antfarm.core.planner import resolve_dependencies
    resolve_dependencies(tasks, child_ids)

    # Generate warnings
    warnings = engine.generate_warnings(result) if hasattr(engine, 'generate_warnings') else []
    if isinstance(warnings, list):
        warn_strs = [str(w) for w in warnings]
    else:
        warn_strs = []

    # Carry each child task
    created_ids = []
    failed_ids = []
    for i, proposed_task in enumerate(tasks):
        child_id = child_ids[i]
        payload = proposed_task.to_carry_dict(child_id)

        # Guardrail: no recursive plans
        payload["capabilities_required"] = []

        # Lineage metadata
        payload["spawned_by"] = {
            "task_id": parent_id,
            "attempt_id": attempt_id,
        }

        try:
            self.colony.carry(
                task_id=child_id,
                title=payload["title"],
                spec=payload["spec"],
                depends_on=payload.get("depends_on", []),
                touches=payload.get("touches", []),
                priority=payload.get("priority", 10),
                complexity=payload.get("complexity", "M"),
                spawned_by=payload["spawned_by"],  # lineage persisted
            )
            created_ids.append(child_id)
            logger.info("carried child task %s", child_id)
        except Exception as exc:
            # 409 = already exists (idempotent retry)
            if "409" in str(exc) or "already exists" in str(exc):
                created_ids.append(child_id)
                logger.info("child task %s already exists (idempotent)", child_id)
            else:
                logger.warning("failed to carry child %s: %s", child_id, exc)
                failed_ids.append(child_id)

    # Partial failure check: if any non-idempotent carry failed, do NOT harvest.
    # Trail the failures and return None — task stays active for operator/doctor.
    if failed_ids:
        logger.warning("plan partial failure: %d/%d tasks failed",
                        len(failed_ids), len(tasks))
        with contextlib.suppress(Exception):
            self.colony.trail(
                task["id"], self.worker_id,
                f"plan partial failure: {len(created_ids)} created, "
                f"{len(failed_ids)} failed: {', '.join(failed_ids)}",
            )
        return None

    # Build dependency summary
    dep_pairs = []
    for i, t in enumerate(tasks):
        for dep in t.depends_on:
            dep_pairs.append(f"{dep} → {child_ids[i]}")
    dep_summary = ", ".join(dep_pairs) if dep_pairs else "all parallel"

    return {
        "created_ids": created_ids,
        "warnings": warn_strs,
        "dep_summary": dep_summary,
    }
```

#### 3e. Add `_parse_plan_result()` helper

```python
def _parse_plan_result(self, output: str) -> str | None:
    """Extract content between [PLAN_RESULT] tags."""
    import re
    match = re.search(
        r"\[PLAN_RESULT\]\s*(.*?)\s*\[/PLAN_RESULT\]",
        output, re.DOTALL,
    )
    return match.group(1) if match else None
```

---

## 4. PlannerEngine Refactor

### File: `antfarm/core/planner.py`

The existing PlannerEngine already has the methods we need. Verify these are callable independently (not tied to the full `plan()` flow):

- `parse_structured_plan(text)` — parses JSON array into ProposedTasks ✅
- `validate_plan(result)` — checks titles, specs, deps, complexity ✅  
- `_detect_cycles()` — called inside validate_plan ✅
- `resolve_dependencies(tasks, task_ids)` — converts index deps ✅
- `generate_warnings(result)` — scope overlap + hotspot warnings

If `generate_warnings` needs a `PlanResult` object, make it accept a list of tasks directly. Minor refactor if needed.

**Key:** The planner worker calls these functions, NOT `_call_agent()`. The worker's own claude-code agent does the AI work. PlannerEngine provides validation only.

---

## 5. Lineage Metadata

### Approach: explicit `spawned_by` field

Add `spawned_by` as a first-class field to ensure it persists through carry.

#### File: `antfarm/core/serve.py` — update CarryRequest

```python
class CarryRequest(BaseModel):
    id: str
    title: str
    spec: str
    complexity: str = "M"
    priority: int = 10
    depends_on: list[str] = []
    touches: list[str] = []
    capabilities_required: list[str] = []
    created_by: str = "api"
    spawned_by: dict | None = None  # NEW: lineage to parent plan task
```

In `carry_task()` endpoint, pass spawned_by through to the task dict:
```python
if req.spawned_by:
    task["spawned_by"] = req.spawned_by
```

#### File: `antfarm/core/colony_client.py` — update carry()

Add `spawned_by` parameter:
```python
def carry(self, ..., spawned_by: dict | None = None) -> dict:
    payload = {...}
    if spawned_by:
        payload["spawned_by"] = spawned_by
    ...
```

This ensures lineage data survives the full carry → API → backend → JSON file chain.

---

## 6. TUI Changes

### File: `antfarm/core/tui.py`

#### Show `[plan]` badge in Waiting: New and Building

Detect plan tasks from capabilities, not prefix:

```python
is_plan = "plan" in task.get("capabilities_required", [])
badge = " [plan]" if is_plan else ""
```

#### Show "spawned N tasks" for done plan tasks

In `_render_recently_merged()` or a dedicated done-plans section:

```python
is_plan = "plan" in task.get("capabilities_required", [])
if is_plan:
    artifact = ...  # get from current attempt
    count = artifact.get("task_count", 0)
    if count:
        merged_text = f"spawned {count} tasks"
```

#### Soldier skips plan tasks for review

Plan tasks produce tasks, not code. Soldier should NOT create review tasks for them. In `process_done_tasks()`:

```python
# Skip plan tasks — they don't need review or merge
if "plan" in task.get("capabilities_required", []):
    continue
```

Plan tasks stay in `done/` as a record of what was planned. They are not merged (no git integration happened).

---

## 7. Tests

### File: `tests/test_worker.py` — additions

```
test_planner_prompt_includes_plan_instructions
    — plan task gets planning-specific prompt with [PLAN_RESULT] tags

test_process_plan_output_valid
    — valid JSON with 3 tasks → 3 child tasks carried with deterministic IDs

test_process_plan_output_invalid_json
    — malformed JSON → returns None, trail entry logged

test_process_plan_output_no_tags
    — output without [PLAN_RESULT] tags → returns None

test_process_plan_output_max_children
    — 15 tasks → rejected (max 10), trail entry logged

test_process_plan_output_deterministic_ids
    — parent "plan-auth" → children "task-auth-01", "task-auth-02", etc. (NOT plan-prefixed)

test_process_plan_output_partial_failure_aborts
    — 3 tasks, 1 carry fails (non-409) → returns None, trails failure, task stays active

test_process_plan_output_idempotent_retry
    — carry 3 tasks, simulate crash, retry → no duplicates (409 handled)

test_process_plan_output_no_recursive_plans
    — child tasks have capabilities_required=[] (no plan capability)

test_process_plan_output_lineage
    — child tasks have spawned_by field with parent task_id + attempt_id

test_process_plan_output_dep_resolution
    — task 2 depends on ["1"] → resolved to "plan-auth-01"

test_process_plan_output_cycle_detection
    — circular deps → rejected, trail entry logged
```

### File: `tests/test_scheduler.py` — additions

```
test_planner_worker_only_forages_plan_tasks
    — worker with capabilities=["plan"], regular task → not foraged

test_planner_worker_forages_plan_task
    — worker with capabilities=["plan"], plan task → foraged
```

### File: `tests/test_cli.py` — additions

```
test_carry_type_plan_adds_prefix
    — carry --type plan --id auth → task ID is "plan-auth"

test_carry_type_plan_adds_capability
    — carry --type plan → capabilities_required includes "plan"

test_worker_start_type_planner_adds_capability
    — worker start --type planner → capabilities includes "plan"
```

### File: `tests/test_planner_worker_e2e.py` — new

```
test_e2e_plan_to_build_flow
    — carry plan task → planner worker decomposes → child tasks appear in queue
    — verify: deterministic IDs, deps resolved, spawned_by present
    — verify: plan task harvested with artifact listing created IDs
```

---

## 8. Files Changed Summary

| File | Change |
|------|--------|
| `antfarm/core/cli.py` | `--type plan` on carry, `planner` worker type |
| `antfarm/core/worker.py` | Planner mode: prompt, `_process_plan_output()`, plan tag parsing |
| `antfarm/core/planner.py` | Verify/minor refactor for reusable validation |
| `antfarm/core/tui.py` | `[plan]` badge, spawned count in merged |
| `antfarm/core/serve.py` | Add `spawned_by` to CarryRequest, pass through to task dict |
| `antfarm/core/colony_client.py` | Add `spawned_by` param to carry() |
| `antfarm/core/soldier.py` | Skip plan tasks in process_done_tasks() |
| `tests/test_worker.py` | ~11 new tests for planner flow |
| `tests/test_scheduler.py` | 2 new tests for plan task routing |
| `tests/test_cli.py` | 3 new tests for carry --type plan |
| `tests/test_planner_worker_e2e.py` | 1 new e2e test |

---

## Key Design Decisions

1. **Inline agent, shared validation.** The worker's claude-code agent does the AI planning. PlannerEngine provides deterministic validation only. No nested subprocess.

2. **Deterministic child IDs.** `task-{slug}-01` format (NOT `plan-` prefixed). If the planner crashes and retries, carry() returns 409 for already-created tasks. No duplicates.

3. **Plan detection via capabilities, not prefix.** `"plan" in task.capabilities_required` is the authoritative check. String prefix is only used for parent plan task IDs (created by CLI). Child tasks are never plan-prefixed.

4. **Lineage via explicit `spawned_by` field.** Added to CarryRequest model and ColonyClient.carry(). Persisted on the task JSON. TUI reads it for lineage display.

5. **Partial failure aborts harvest.** If any non-idempotent child carry fails, the plan task is NOT harvested. It stays active for doctor/operator recovery. Trail records what succeeded and what failed.

6. **Max 10 children.** Hard guardrail. Prevents colony flooding from bad AI output. Configurable later.

7. **No recursive plans.** Child tasks have `capabilities_required=[]`. A plan task cannot spawn another plan task. Prevents infinite decomposition loops.

8. **Scheduler reuse.** The existing specialized-worker logic already handles plan workers. `capabilities=["plan"]` is not `"builder"`, so the worker only forages plan tasks. Zero scheduler changes needed.

9. **PlannerEngine reuse.** Parsing, validation, cycle detection, and dep resolution are already built. The planner worker calls them — doesn't reimplement.

10. **Plan tasks skip review and merge.** Plan tasks produce tasks, not code. Soldier skips them in `process_done_tasks()` — no review task created. Plan tasks stay in `done/` as a planning record. They are never "merged" because no git integration happened.

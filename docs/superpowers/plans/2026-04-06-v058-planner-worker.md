# v0.5.8 — Planner Worker Implementation Plan

**Status:** DRAFT — pending approval
**Derived from:** SPEC_v05.md v0.5.8 section
**Goal:** Remove the human from the front of the pipeline. Operator carries a plan task, planner worker decomposes it into sub-tasks, builders build, reviewers review, Soldier merges. Spec in, merged code out.

---

## Scope

- Planner worker type (`--type planner`, `capabilities=["plan"]`)
- Plan task creation (`antfarm carry --type plan`)
- Planner mode in worker runtime (forage plan task → agent decomposes → validate → carry children)
- Deterministic child IDs (`plan-{parent}-01`, `plan-{parent}-02`)
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

After forage, check if task is a plan task:

```python
task_id = task["id"]
is_plan = task_id.startswith("plan-")
is_review = task_id.startswith("review-")
```

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

    # Generate deterministic child IDs
    parent_id = task["id"]
    child_ids = [f"{parent_id}-{i:02d}" for i in range(1, len(tasks) + 1)]

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

### Approach: `spawned_by` in task spec payload

The simplest approach without model changes: include `spawned_by` as an extra field in the task dict. The carry endpoint accepts arbitrary fields (they're passed through to the JSON file). No backend or model changes needed.

```python
payload["spawned_by"] = {
    "task_id": "plan-auth",
    "attempt_id": "att-001",
}
```

The TUI can read this field to show lineage. The `carry()` endpoint stores whatever fields are in the payload.

### CarryRequest in serve.py

Check if `CarryRequest` model strips extra fields. If it does, we need to either:
- Add `spawned_by: dict | None = None` to CarryRequest
- Or pass it through as part of the task dict

Read serve.py to verify.

---

## 6. TUI Changes

### File: `antfarm/core/tui.py`

#### Show `[plan]` badge in Waiting: New and Building

In `_render_waiting_new()` and `_render_building()`, check if task ID starts with `plan-`:

```python
badge = " [plan]" if task.get("id", "").startswith("plan-") else ""
```

Append to task title display.

#### Show "spawned N tasks" in Recently Merged for plan tasks

In `_render_recently_merged()`, for plan tasks:

```python
if task.get("id", "").startswith("plan-"):
    # Get created count from artifact
    for a in task.get("attempts", []):
        if a.get("status") == "merged":
            artifact = a.get("artifact", {})
            count = artifact.get("task_count", 0)
            if count:
                merged_text = f"spawned {count} tasks"
```

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
    — parent "plan-auth" → children "plan-auth-01", "plan-auth-02", etc.

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
| `antfarm/core/serve.py` | Maybe: add `spawned_by` to CarryRequest |
| `tests/test_worker.py` | ~11 new tests for planner flow |
| `tests/test_scheduler.py` | 2 new tests for plan task routing |
| `tests/test_cli.py` | 3 new tests for carry --type plan |
| `tests/test_planner_worker_e2e.py` | 1 new e2e test |

---

## Key Design Decisions

1. **Inline agent, shared validation.** The worker's claude-code agent does the AI planning. PlannerEngine provides deterministic validation only. No nested subprocess.

2. **Deterministic child IDs.** `plan-{parent}-01` format. If the planner crashes and retries, carry() returns 409 for already-created tasks. No duplicates.

3. **Lineage via `spawned_by` field.** Extra dict on the task JSON. No model changes. TUI reads it for "spawned N tasks" display.

4. **Max 10 children.** Hard guardrail. Prevents colony flooding from bad AI output. Configurable later.

5. **No recursive plans.** Child tasks have `capabilities_required=[]`. A plan task cannot spawn another plan task. Prevents infinite decomposition loops.

6. **Scheduler reuse.** The existing specialized-worker logic already handles plan workers. `capabilities=["plan"]` is not `"builder"`, so the worker only forages plan tasks. Zero scheduler changes needed.

7. **PlannerEngine reuse.** Parsing, validation, cycle detection, and dep resolution are already built. The planner worker calls them — doesn't reimplement.

8. **Plan task harvests like any task.** The plan task goes through the normal harvest flow. Its artifact contains the list of created task IDs instead of code changes. Soldier will see it as "done" and (if review is required) create a review task for it — but review of a plan task is optional since it didn't produce code.

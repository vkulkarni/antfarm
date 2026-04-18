# issue #320: autoscaler depth-aware scaling with hysteresis

The current autoscaler spawns up to `max_parallel_builders` builders and keeps
them alive. It does **not** scale with the actual ready-queue depth, does **not**
cap its growth under sustained contention, and does **not** scale down lazily
after work drains.

Result from Phase 4 M1 (v2 spec, 2026-04-17):
```
T+5min state:
  ready=5  active=3  done=7
  workers=7  (3 builders + 4 reviewers)
  max_parallel_builders=3
```

At T+5min there were 5 ready-and-unblocked tasks and only 3 concurrent
builders — the autoscaler clamped at the mission's `max_parallel_builders`.
An extra 1-2 builders in that window would have drained the queue faster.
This spec makes the autoscaler **depth-aware with hysteresis**.

## Acceptance criteria

### 1. Configurable depth factor

- New autoscaler config field `builder_depth_factor: float = 0.5`.
- New autoscaler config field `builder_scale_up_threshold: int = 2`.
- New autoscaler config field `builder_scale_down_idle_seconds: float = 30.0`.

### 2. Depth-aware target computation

Replace the current fixed-ceiling logic with:

```
ready_unblocked = count tasks where:
  status == READY
  AND all depends_on are in done_task_ids or merged_task_ids
  AND not infra tasks (plan-*, review-*)

target_builders = min(
    mission.max_parallel_builders,
    max(1, ceil(ready_unblocked * builder_depth_factor)),
)
```

When `ready_unblocked == 0`, target_builders = 0 (eventually lets idle
builders retire).

### 3. Scale up aggressively

When `current_builders < target_builders` AND
`ready_unblocked >= builder_scale_up_threshold`, spawn the delta immediately.
No cooldown on scale-up.

### 4. Scale down lazily

An idle builder is eligible for retirement when:
- It has had no claimed task for `builder_scale_down_idle_seconds`.
- `current_builders > target_builders`.

Retire ONE builder per autoscaler tick — do not burst-kill workers.
Retirement is the existing "worker retirement" path (gracefully via tmux
session kill + deregister_worker).

### 5. Honor the mission max as a hard ceiling

`mission.config.max_parallel_builders` is a HARD CAP. The depth-aware
target never exceeds it.

### 6. Leave reviewer scaling unchanged (out of scope)

This spec is for builder workers. Reviewer/planner scaling is untouched.

### 7. Tests

`tests/test_autoscaler.py`:

- `test_depth_aware_target_when_queue_is_deep`: mock a mission with
  `max_parallel_builders=5`, set `ready_unblocked=8`,
  `builder_depth_factor=0.5` → expect target=4 (min of 5 and ceil(8*0.5)=4).
- `test_depth_aware_target_clamps_to_mission_cap`: `max=3`, `ready_unblocked=10` → target=3.
- `test_depth_aware_target_at_least_one_when_work_exists`:
  `ready_unblocked=1`, factor=0.5 → target=1 (floor protection).
- `test_depth_aware_target_zero_when_no_work`:
  `ready_unblocked=0` → target=0.
- `test_scale_up_respects_threshold`:
  `ready_unblocked=1`, `builder_scale_up_threshold=2` → do NOT spawn
  (prevents flutter on a single in-flight task).
- `test_scale_down_only_after_idle_period`: current=3, target=1, builder
  has been idle 10s, threshold 30s → do NOT retire yet.
- `test_scale_down_retires_one_per_tick`: current=5, target=1, all workers
  idle 60s → retire exactly 1 builder (the oldest or first in some
  deterministic order).
- `test_infra_tasks_excluded_from_depth`: ready queue has 3 plan-* tasks
  + 0 task-* → target=0 (infra tasks don't count).

### 8. Version bump

- Bump `pyproject.toml` `version` from `0.6.8` to `0.6.9`.

### 9. Scope boundaries

- Do NOT change reviewer or planner scaling.
- Do NOT introduce multi-node scaling — single-host only.
- Do NOT change the hard `max_parallel_builders` semantics.
- Keep worker-retirement semantics unchanged — scale-down uses the
  existing retirement path.

## Files likely touched

- `antfarm/core/autoscaler.py` — new config fields + scaling logic
- `antfarm/core/scheduler.py` (possibly) — `ready_unblocked` count helper
  if one doesn't already exist
- `pyproject.toml` — version bump
- `tests/test_autoscaler.py` — new tests

## Non-goals

- Predictive scaling (anticipating tasks before they're carried).
- Heterogeneous workers (agent-type preferences).
- Rate-limit-aware backoff (separate issue).
- Multi-node autoscaling (separate issue).

Closes #320.

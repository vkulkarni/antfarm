# Antfarm v0.5 Roadmap

**Goal:** Turn Antfarm into the safest, simplest way to run multiple coding agents against one real repo without merge chaos.

**Philosophy:** Consolidation, not expansion. Antfarm should be a repo execution coordinator, not an agent civilization simulator. Deterministic core, AI at the edges where judgment helps.

Source: ChatGPT principal-engineer review of v0.4 codebase.

---

## P0 — Must Do

### 1. One Canonical Scheduler (#72)
Remove inline scheduling from FileBackend.pull(). All task selection flows through scheduler.select_task(). Prevents behavior drift.

### 2. Structured Task Output Contract
Every worker completion emits: task_id, attempt_id, files_changed, summary, tests_run, test_results, lint_results, known_risks, merge_readiness. Makes Soldier deterministic. Reduces human review time.

### 3. Lightweight Repo Memory
Local SQLite or JSONL in .antfarm/. Store: repo facts, task outcome summaries, failure patterns, common commands, hot files. Workers stop rediscovering basics. No vector DB.

### 4. Better Planner/Decomposer
Input: issue/spec/bug report. Output: tasks with dependencies, touches, risk levels, validation commands, merge order. This is the biggest usefulness unlock.

### 5. Conflict Prevention Layer
Overlap warnings on touches, hotspot detection, optional serialization for risky modules, "likely conflict" flags, file/module claim hints. Higher value than fancy dashboards.

## P1 — Strongly Recommended

### 6. Operator Inbox
CLI/TUI view answering: what's blocked? what failed? what's stale? what needs merge? what needs human decision? what's colliding?

### 7. Review Pack Generation
On task completion: summary, files changed, checks run, risks, suggested review focus. Humans review faster.

### 8. Retry and Failure Taxonomy
Classify failures: agent failure, repo/setup failure, test failure, flaky infra, merge conflict, invalid decomposition. Helps routing and future planning.

### 9. GitHub Flow Tightening
Issue→task mapping, task outcome→PR comment, merge-ready signaling, human decision points. Not more GitHub features — tighten the core loop.

## P2 — If Time Remains

### 10. Worker Specialization (language, repo area, tool availability)
### 11. Minimal Reviewer Assistant (diff summary, risky files, missing tests)
### 12. Importers/Bootstrap Helpers

## Explicitly Avoid

- More backends (file + GitHub is enough)
- Overbuild auth/platform admin
- LLM-first Soldier
- Recursive agent orchestration
- Vector DB / semantic memory
- Web app (CLI + TUI is enough)

## Release Slices

### v0.5.1
- Canonical scheduler
- Thin backend cleanup
- Task artifact contract
- Basic operator inbox

### v0.5.2
- Repo memory
- Task summary persistence
- Hotspot detection
- Better retries/failure classification

### v0.5.3
- Planner/decomposer
- Issue/spec ingestion
- Validation command inference
- Improved merge gating

## Success Criteria

A. Feature decomposition: break spec into 5-10 tasks with deps and overlap detection
B. Safe parallel execution: 3-5 workers, no stepping on each other, recover stale tasks
C. Reviewable output: summary, files, checks, risks per task
D. Deterministic merging: only when deps complete, artifact complete, checks passed
E. Useful memory: second run in same repo is better than first

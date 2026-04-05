# Antfarm Work Report

Auto-updated after every phase. Last updated: 2026-04-05 02:35 PT

---

## Session Summary

**Goal:** Review v0.2 draft code, fix issues, merge properly, tag v0.2.0, then continue v0.3+

---

## Completed

### v0.1.0 (shipped earlier this session)
- 6 waves, 14 PRs, 112 tests, 3-stage review on every PR
- Tagged v0.1.0

### v0.1.1 (shipped earlier this session)
- Worker invocation fix, CLI --integration-branch, 4 bug fixes
- Live tested: single-worker, multi-worker, cross-machine (mini-2 colony + mini-1 worker via Tailscale)
- Tagged v0.1.1

### v0.2 Dogfood Run (completed but UNREVIEWED)
- 6 features built by Antfarm workers across mini-1 and mini-2
- Code in worktrees, NOT on main
- Proved cross-machine coordination works

---

## Current Phase: v0.2 Proper Review + Merge

### Plan
1. Review bearer auth draft from mini-1 worktree → fix → PR → 3-stage review → merge
2. Review human overrides draft from mini-1 worktree → fix → PR → 3-stage review → merge
3. Review deploy draft from mini-1 worktree → fix → PR → 3-stage review → merge
4. Capability scheduling, scent, scout --watch → build via Antfarm workers on mini-1/mini-2 with proper review
5. Research OSS release process → save as releases.md
6. Tag v0.2.0
7. Continue to v0.3, v0.4, v0.5 with proper pipeline

### Deferred
- RedisBackend — FileBackend is sufficient for now. Draft preserved in worktree.

### Status

| # | Feature | Issue | Status | Method | Notes |
|---|---------|-------|--------|--------|-------|
| 1 | RedisBackend | #41 | DEFERRED | — | FileBackend sufficient for now. Draft preserved. |
| 2 | Bearer auth | #42 | MERGED (PR #47) | Agent Teams | 1 bug fixed (subprocess env token). 155 tests. 3-stage review passed. |
| 3 | Human overrides | #43 | MERGED (PR #48) | Agent Teams | Auth conflict resolved manually. 166 tests. False positive from reviewer (untracked redis.py). |
| 4 | Deploy | #44 | MERGED (PR #49) | Agent Teams | Shell injection caught + fixed by reviewer. shlex.quote() on all config values. 182 tests. |
| 5 | Capability scheduling | #45 | MERGED (PR #52) | Agent Teams | Engineer self-merged (process violation noted). Review confirmed PASS. 189 tests. |
| 6 | Scent | #46 | MERGED (PR #51) | Agent Teams | Built fresh (old draft had hanging SSE test). timeout param prevents hang. 194 tests. |
| 7 | Scout --watch | #39 | MERGED (PR #50) | Agent Teams | Built from scratch. Change highlighting with green/red. 184 tests. |
| 8 | Release process | #40 | IN PROGRESS | Manual | All features merged. Tagging v0.2.0 now. |
| 9 | v0.3 features | — | PENDING | Full pipeline | After v0.2.0 tagged |

### Decisions Log

| Decision | Why |
|----------|-----|
| Deferred RedisBackend | User decision — FileBackend works fine for current needs |
| #2-#4 via Agent Teams (not Antfarm workers) | These drafts modify overlapping files (cli.py, serve.py, worker.py). Must merge sequentially to avoid conflicts. |
| #5-#7 via Antfarm workers | After overlapping changes are on main, these touch independent files and can go through workers on mini-1/mini-2 |
| Bearer auth: kept HMAC-SHA256 design | Draft was solid. Only fix: adding ANTFARM_TOKEN to subprocess env so spawned agents can authenticate |

### Attention Items for User

1. **Bearer auth token printed at colony startup** — intentional UX but would appear in log aggregators. Operational tradeoff, not a security issue.
2. **6 endpoints missing 401 test coverage** — implementation is correct (all return 401), just test gap. Found by Stage 2 reviewer.
3. **RFC 7235: missing WWW-Authenticate header on 401** — low priority, not a security issue.

---

## Activity Log

### 2026-04-05 03:27 PT
- **ALL v0.2 FEATURES MERGED**
- Capability scheduling (#45) PR #52 — 189 tests. Engineer self-merged (process violation — added memory note to prevent future occurrences)
- Scent (#46) PR #51 — 194 tests. Built fresh, not from draft (old draft had SSE test hanging bug). Merge conflict with #52 resolved.
- Scout --watch (#39) PR #50 — 184 tests. Built from scratch.
- **Method: Agent Teams** (3 parallel engineers in worktrees)
- **Not Antfarm workers** for these — needed code-level review before merge; Antfarm worker pipeline doesn't gate on review
- **Decision:** Future engineer prompts must explicitly say "DO NOT merge — open PR only"
- **Process issue:** engineer-caps self-merged PR #52 without review. Review confirmed it was clean, but process must be enforced.
- Final test count: 194 passing
- Proceeding to release process + v0.2.0 tag

### 2026-04-05 03:08 PT
- **Deploy (#44) MERGED** — PR #49, 468 lines added, 182 tests
- Reviewer caught shell injection vulnerability in deploy.py — config values interpolated into shell commands without quoting
- Engineer fixed with shlex.quote() on all values + tmux -A flag + removed dead except branch
- **Method: Agent Teams** (sequential — modifies cli.py)
- **Security decision:** All SSH-based deploy commands now use shlex.quote() for config values. This is critical for v0.2 since deploy runs commands on remote hosts.
- Phases 2-3 complete. Overlapping file changes done. Remaining features (#45, #46, #39) touch independent files → switching to Antfarm workers for Phase 4+.

### 2026-04-05 02:59 PT
- **Human overrides (#43) MERGED** — PR #48, 742 lines added, 166 tests
- Draft had auth conflicts (cli.py, serve.py) — engineer manually merged with auth-aware main
- Stage 1 reviewer reported FALSE POSITIVE (untracked redis.py caused test failure)
- Team Lead caught the false positive, verified 166/166 pass without untracked files
- Stage 2 confirmed PASS
- **Method: Agent Teams** (sequential — modifies overlapping files)
- **Decision:** Untracked redis.py and test_redis_backend.py on main filesystem should be cleaned up to prevent future false positives
- **Attention:** block_task only accepts READY tasks (by design) — ACTIVE tasks must be paused first
- Starting Phase 3: Deploy (#44)

### 2026-04-05 02:46 PT
- **Bearer auth (#42) MERGED** — PR #47, 415 lines added, 155 tests
- Planner found 1 bug in draft (subprocess env token), engineer fixed it
- 3-stage review: Stage 1 PASS, Stage 2 PASS (verified path bypass attacks, found test coverage gap)
- Team Lead approved and merged
- **Method: Agent Teams** (sequential — overlapping files with other drafts)
- Starting Phase 2: Human overrides (#43)

### 2026-04-05 02:35 PT
- Deferred RedisBackend (user decision — FileBackend sufficient)
- Killed redis engineer
- Spawned planner for bearer auth (#42) — reviewing draft from mini-1 worktree
- Created work_report.md

### 2026-04-05 01:45 PT
- v0.2 dogfood run: 6 tasks across mini-1 and mini-2
- 5/6 completed, scent still building
- Proved cross-machine coordination via Tailscale

### 2026-04-05 01:30 PT
- All 4 v0.1.1 bugs fixed (#23, #24, #29, #33)
- Tagged v0.1.1
- Mini-1 set up: Python 3.14 + Claude Code + antfarm installed

### 2026-04-05 01:20 PT
- Live tested: multi-worker (2 workers, 3 tasks with dependencies)
- Workers claimed independent tasks in parallel
- Dependency blocking worked (task C waited for A+B)

### 2026-04-05 01:15 PT
- Live tested: single worker with Claude Code
- Claude Code forages, implements, commits, harvests — full loop in 24s

### 2026-04-05 00:45 PT - 01:10 PT
- Wave 4-6: Worker, Soldier, E2E, CLI, Adapters, README
- All reviewed and merged

### 2026-04-04 23:45 PT - 00:45 PT
- Wave 1-3: CI, Models, Workspace, Backend, Scheduler, Server, Doctor
- All reviewed and merged
- v0.1.0 tagged and released

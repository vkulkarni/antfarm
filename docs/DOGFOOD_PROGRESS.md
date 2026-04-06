# Antfarm Dogfood Progress — v0.5.7x

**Started:** 2026-04-05 ~17:00 PT
**Goal:** Use antfarm to build antfarm improvements (TUI + bug fixes)
**Colony:** mini-2:7433, v0.5.0, 493 tests baseline
**Workers:** mini-2/bug-fixer + mini-1/tui-builder (2 nodes, 2 workers)

## Results

All 9 tasks completed by 2 workers across 2 nodes. Workers produced real commits and pushed branches to origin. Review flow did NOT trigger (Soldier wasn't started — bug #99).

### Wave A: TUI Enhancements
| Task | Issue | Status | Worker | Branch |
|------|-------|--------|--------|--------|
| task-fix-tui-summary | #90 | DONE | mini-2/bug-fixer | feat/task-fix-tui-summary-* |
| task-tui-nodes | #96 | DONE | mini-2/bug-fixer | feat/task-tui-nodes-* |
| task-tui-dep-graph | #86 | DONE | mini-1/tui-builder | feat/task-tui-dep-graph-* |
| task-tui-progress | #87 | DONE | mini-2/bug-fixer | feat/task-tui-progress-* |
| task-tui-heatmap | #88 | DONE | mini-1/tui-builder | feat/task-tui-heatmap-* |
| task-tui-timeline | #89 | DONE | mini-1/tui-builder | feat/task-tui-timeline-* |

### Wave B: Bug Fixes
| Task | Issue | Status | Worker | Branch |
|------|-------|--------|--------|--------|
| task-fix-hotspots | #91 | DONE | mini-2/bug-fixer | feat/task-fix-hotspots-* |
| task-fix-datadir | #92 | DONE | mini-1/tui-builder | feat/task-fix-datadir-* |
| task-fix-plan-deps | #93 | DONE | mini-2/bug-fixer | feat/task-fix-plan-deps-* |

## Antfarm Bugs Found During Dogfooding

| Bug | Issue | Severity | Fixed? | Notes |
|-----|-------|----------|--------|-------|
| Worker doesn't register node | #98 | Medium | YES | Fixed: auto register_node() before register_worker() |
| Soldier not auto-started with colony | #99 | High | No | Entire review flow bypassed |
| No notification on task completion | #100 | Medium | No | Polling only, discovered done tasks late |
| Workers exit silently | #101 | Low | No | No trail entry or status update on exit |
| Review flow needs turnkey setup | #102 | High | No | Too many manual steps for review loop |

## Timeline
- 17:00 — Colony restarted with v0.5.0, planning started
- 17:15 — 9 tasks carried with detailed specs
- 17:16 — Worker bug-fixer started on mini-2
- 17:17 — Issues #96, #97 filed (TUI node/worker display)
- 17:22 — Node.js + Claude CLI installed on mini-1
- 17:23 — Worker tui-builder started on mini-1 via Tailscale
- 17:24 — 3 tasks done, system working
- 17:27 — Bug #98 found and fixed (node not auto-registered)
- 17:30 — 6 tasks done, 2 active
- ~17:40 — All 9 tasks completed (discovered later — no notification!)
- 17:45 — 4 bugs filed (#99-#102): Soldier, notifications, silent exit, review setup
- 17:46 — Manual review of worker branches needed (review flow bypassed)

## Key Learnings
1. **Antfarm works for real parallel development** — 2 workers across 2 nodes via Tailscale processed 9 tasks autonomously
2. **The Soldier must be integrated into the colony** — a separate process is too easy to forget
3. **Notifications are critical** — polling-only is unacceptable for production use
4. **Review flow needs to be turnkey** — if it requires 3 manual setup steps, it won't be used
5. **Worker specs worked** — detailed specs with file paths, code snippets, and commit messages produced correct commits

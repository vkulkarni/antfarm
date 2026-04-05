# Antfarm — Development Workflow

Standards for contributing to Antfarm as an open-source project.

---

## Branching Strategy

```
main              ← protected, always releasable
  ├── feat/...    ← feature branches
  ├── fix/...     ← bug fixes
  └── chore/...   ← infra, deps, docs
```

### Rules

- **`main`** is the only long-lived branch. Protected. Always healthy. Releasable when tagged.
- All work happens on **short-lived branches** off `main`.
- All PRs target `main`. Squash merge by default.
- Tag releases from `main` when ready. Not every merge is a release.
- Before merge: CI green, docs/tests updated when behavior changed, review for non-trivial PRs.
- **PR review before merge** — always run a review pass (read the full diff, run the test suite, check edge cases) before merging. Never skip this step. Never trust self-reported test results.
- **Never push to main without permission** — merges to `main` and pushes only when explicitly requested. Never merge to `main` autonomously.

### Branch Naming

```
feat/<short-description>     # new feature
fix/<short-description>      # bug fix
chore/<short-description>    # deps, config, CI
refactor/<short-description> # code improvement, no behavior change
test/<short-description>     # adding/updating tests
docs/<short-description>     # documentation
```

Examples:
```
feat/file-backend
feat/scheduler
fix/stale-guard-recovery
chore/ci-setup
refactor/models-cleanup
test/e2e-full-loop
```

---

## Commit Conventions

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

body (optional)

footer (optional)
```

### Types

| Type | When |
|------|------|
| `feat` | New feature |
| `fix` | Bug fix |
| `test` | Adding or updating tests |
| `chore` | Setup, config, infra, deps |
| `refactor` | Code improvement, no behavior change |
| `docs` | Documentation only |

### Scopes

| Scope | What |
|-------|------|
| `models` | Task, Worker, Node, Attempt dataclasses |
| `backend` | TaskBackend interface and implementations |
| `scheduler` | Task scheduling logic |
| `server` | Colony API server |
| `worker` | Worker runtime |
| `workspace` | Git worktree management |
| `soldier` | Merge queue and integration engine |
| `doctor` | Pre-flight checks and recovery |
| `cli` | CLI commands |
| `adapter` | Agent adapters |
| `ci` | CI/CD configuration |

### Examples

```
feat(backend): implement FileBackend with atomic task claiming
feat(scheduler): add scope-aware task selection
fix(worker): handle workspace cleanup on crash
test(soldier): add kickback attempt superseding test
chore(ci): add GitHub Actions workflow for pytest + ruff
docs: update README with quick start
```

### Rules

- **One logical change per commit.** Don't bundle unrelated work.
- **Write in imperative mood:** "add", "fix", "update" — not "added", "fixes", "updated"
- Issue references are optional in commits. PRs are the right place for issue linkage.

---

## Recommended Initial PR Sequence

The initial PRs should build the project in a logical order. Each PR should leave `main` in a working state.

```
PR 1:  chore: repo bootstrap (pyproject.toml, LICENSE, .gitignore, ruff config)
PR 2:  docs: add SPEC.md, IMPLEMENTATION.md, DEVELOPMENT.md, README.md
PR 3:  chore(ci): GitHub Actions CI workflow (lint + test guardrails from the start)
PR 4:  feat(models): Task, Attempt, Worker, Node dataclasses + enums
PR 5:  feat(backend): TaskBackend ABC + FileBackend implementation
PR 6:  test(backend): FileBackend unit tests
PR 7:  feat(scheduler): scope-aware task scheduler + tests
PR 8:  feat(server): colony API server + tests
PR 9:  feat(workspace): git worktree manager + tests
PR 10: feat(worker): worker runtime lifecycle + tests
PR 11: feat(doctor): pre-flight checks + stale recovery + tests
PR 12: feat(soldier): merge queue + integration engine + tests
PR 13: test(e2e): end-to-end integration test
PR 14: feat(cli): wire up all v0.1 commands + smoke tests
PR 15: feat(adapter): Claude Code reference adapter
PR 16: feat(adapter): generic curl adapter
PR 17: docs: finalize README with real quick start
```

This is a recommended order, not a rigid prescription. Some PRs may be combined or reordered based on what makes sense during implementation.

---

## Pull Request Process

### Creating a PR

1. Branch from `main`
2. Implement with focused commits (one logical change each)
3. Run tests locally: `pytest tests/ -x -q`
4. Run linter: `ruff check .`
5. Open PR to `main` with:
   - Short title (< 70 chars)
   - Description of what and why
   - Link to issue if applicable
   - Test plan

### PR Template

```markdown
## Summary
- What this PR does (1-3 bullet points)

## Test Plan
- [ ] Unit tests pass
- [ ] Linter passes
- [ ] Manual test: <describe>

## Related Issues
Closes #NNN (if applicable)
```

### Review Checklist

- [ ] Does it do one thing well?
- [ ] Are edge cases handled?
- [ ] Are tests added for new behavior?
- [ ] Does it follow commit conventions?
- [ ] No unnecessary dependencies added?
- [ ] No secrets or credentials?

### Merge

- Squash merge by default (clean history)
- Delete the feature branch after merge

---

## CI Pipeline

### GitHub Actions (`.github/workflows/ci.yml`)

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: pytest tests/ -x -q
```

Single Python version (3.12) for v0.1. Expand matrix later if needed.

---

## Release Process

### Versioning

[Semantic Versioning](https://semver.org/):

```
v0.1.0  — first release (prove core loop)
v0.1.1  — patch (bug fix)
v0.2.0  — minor (new feature: Redis backend, auth, etc.)
v1.0.0  — major (stable API, production-ready)
```

### v0.1 Release Workflow

1. All work merged to `main`, CI green
2. Tag: `git tag v0.1.0 && git push --tags`
3. Create GitHub Release with changelog

PyPI publishing is not required for v0.1. Early users install via:

```bash
git clone https://github.com/vkulkarni/antfarm.git
cd antfarm
pip install -e .
```

PyPI (`pip install antfarm`) will be set up when distribution as a package matters.

### Changelog

Maintain `CHANGELOG.md` with entries for each release:

```markdown
## [0.1.0] - 2026-XX-XX

### Added
- FileBackend with atomic task claiming
- Scope-aware task scheduler
- Colony API server (FastAPI)
- Worker runtime lifecycle
- Soldier integration engine with hard policy rules
- Task attempt model
- Doctor pre-flight checks and stale recovery
- Claude Code reference adapter
- Generic curl adapter
- CLI: colony, join, carry, worker start, forage, trail, harvest, scout, guard, release, doctor
```

---

## Code Style

- **Formatter:** `ruff format`
- **Linter:** `ruff check`
- **Type hints:** use them on public interfaces, optional on internals
- **Docstrings:** Google style, on public functions only
- **Tests:** pytest, one test file per module
- **Comments:** prefer self-explanatory code. Use comments to explain invariants, edge cases, non-obvious decisions, and failure-recovery rationale. Avoid redundant narration.

### Ruff Config

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "W", "UP", "B", "SIM"]
```

---

## Issue Tracking

Use GitHub Issues for all work.

### Labels

| Label | Meaning |
|-------|---------|
| `core` | Core infrastructure (models, backend, scheduler) |
| `server` | Colony API server |
| `worker` | Worker runtime |
| `soldier` | Integration engine |
| `cli` | CLI commands |
| `adapter` | Agent adapters |
| `docs` | Documentation |
| `bug` | Something broken |
| `enhancement` | Improvement to existing feature |
| `good first issue` | Suitable for new contributors |
| `help wanted` | Community contribution welcome |

### Milestones

| Milestone | Scope |
|-----------|-------|
| v0.1.0 | Core loop: carry → forage → work → harvest → integrate |
| v0.2.0 | Redis backend, auth, human overrides, scent, deploy |
| v0.3.0 | TUI dashboard, rate limits, failover |

---

## Project Governance

- **Maintainer:** @vkulkarni
- **Decision model:** Benevolent dictator. Maintainer has final say.
- **Contributions:** Welcome via PR. Open an issue for discussion before large changes.
- **License:** MIT. See LICENSE for details.

---

## Local Development Setup

```bash
# Clone
git clone https://github.com/vkulkarni/antfarm.git
cd antfarm

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest tests/ -x -q

# Lint
ruff check .

# Format
ruff format .
```

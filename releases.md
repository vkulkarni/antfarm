# Antfarm Release Process

Researched from: Kubernetes, FastAPI, Click, httpx, Ruff, and other well-run OSS projects.

---

## Versioning

**Semantic Versioning (semver):** `MAJOR.MINOR.PATCH`

| Version | When |
|---------|------|
| PATCH (0.1.1) | Bug fixes, no new features, backward compatible |
| MINOR (0.2.0) | New features, backward compatible |
| MAJOR (1.0.0) | Breaking changes (API, CLI, config format) |

Pre-1.0: MINOR bumps may include breaking changes (we're still shaping the API).

## Release Checklist

Before every release:

1. All planned issues for the milestone are closed
2. Full test suite passes: `python3.12 -m pytest tests/ -x -q`
3. Lint clean: `python3.12 -m ruff check .`
4. CHANGELOG.md updated with all changes since last release
5. `pyproject.toml` version bumped
6. All machines synced (mini-1, mini-2)
7. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
8. GitHub Release created with changelog
9. (Future) PyPI publish: `python -m build && twine upload dist/*`
10. (After v0.2.0) Lock main branch with branch protection

## CHANGELOG Format

Follow [Keep a Changelog](https://keepachangelog.com/):

```markdown
## [Unreleased]

## [0.2.0] - 2026-04-05

### Added
- feature description

### Changed
- change description

### Fixed
- fix description

### Security
- security fix description
```

Categories: Added, Changed, Deprecated, Removed, Fixed, Security

## Branch Model

- `main` — only long-lived branch, always healthy
- Feature branches → PRs → main
- Tags from main only
- After v0.2.0: branch protection (require PRs + CI)

## Hotfix Process

For critical fixes on a released version:
1. Branch from the release tag: `git checkout -b hotfix/description vX.Y.Z`
2. Fix, test, PR to main
3. Tag new patch: `vX.Y.Z+1`

## PyPI Publishing (future)

When ready (likely v0.3+):
1. Ensure `pyproject.toml` has correct metadata (author, description, classifiers)
2. `python -m build`
3. `twine upload dist/*`
4. Or automate via GitHub Actions on tag push

## Milestone Tracking

Each version has a GitHub milestone. Issues assigned to milestones. Milestone closed when version ships.

## Adopted Practices

| Practice | Source |
|----------|--------|
| Semver | Universal |
| Keep a Changelog format | keepachangelog.com |
| Tag from main only | FastAPI, Ruff |
| GitHub Releases with changelog | Kubernetes, httpx |
| Branch protection after stabilization | All major projects |
| One PR per feature | FastAPI, Click |
| CI must pass before merge | Universal |

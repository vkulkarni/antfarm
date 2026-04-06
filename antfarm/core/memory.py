"""Lightweight repo memory for Antfarm.

Stores trusted facts, task outcomes, hotspot data, failure patterns, and
touch observations in `.antfarm/memory/`. Workers receive repo_facts as
context; scheduler uses hotspots as a weighting signal.

Trust model:
- repo_facts.json — TRUSTED: operator-curated + auto-detected durable facts
- task_outcomes.jsonl — APPEND-ONLY: factual run history
- hotspots.json — HEURISTIC: computed from outcomes, may be noisy
- failure_patterns.json — HEURISTIC: derived failure clusters
- touch_observations.jsonl — HEURISTIC: actual files/scopes touched per task
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class MemoryStore:
    """JSONL-based memory stored in .antfarm/memory/.

    Args:
        root: Path to the .antfarm directory.
    """

    def __init__(self, root: str | Path) -> None:
        self._dir = Path(root) / "memory"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Repo facts (trusted, operator-curated)
    # ------------------------------------------------------------------

    def _facts_path(self) -> Path:
        return self._dir / "repo_facts.json"

    def get_facts(self) -> dict:
        """Return all repo facts."""
        path = self._facts_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def set_fact(self, key: str, value: str) -> None:
        """Set a single repo fact."""
        facts = self.get_facts()
        facts[key] = value
        self._facts_path().write_text(json.dumps(facts, indent=2))

    def detect_facts(self, repo_path: str | Path) -> dict:
        """Auto-detect repo facts from project structure.

        Detects: language, build_command, test_command, lint_command, framework.
        Only sets facts that are not already set (operator overrides win).
        """
        repo = Path(repo_path)
        detected: dict = {}

        if (repo / "pyproject.toml").exists():
            detected.setdefault("language", "python")
            detected.setdefault("test_command", "pytest tests/ -x -q")
            detected.setdefault("lint_command", "ruff check .")
        if (repo / "package.json").exists():
            detected.setdefault("language", "javascript")
            detected.setdefault("test_command", "npm test")
            detected.setdefault("lint_command", "npm run lint")
        if (repo / "Cargo.toml").exists():
            detected.setdefault("language", "rust")
            detected.setdefault("test_command", "cargo test")
            detected.setdefault("lint_command", "cargo clippy")
        if (repo / "go.mod").exists():
            detected.setdefault("language", "go")
            detected.setdefault("test_command", "go test ./...")
            detected.setdefault("lint_command", "golangci-lint run")
        if (repo / "Makefile").exists():
            detected.setdefault("build_command", "make")

        # Only set facts that aren't already operator-set
        current = self.get_facts()
        for k, v in detected.items():
            if k not in current:
                current[k] = v
        self._facts_path().write_text(json.dumps(current, indent=2))
        return detected

    # ------------------------------------------------------------------
    # Task outcomes (append-only run history)
    # ------------------------------------------------------------------

    def _outcomes_path(self) -> Path:
        return self._dir / "task_outcomes.jsonl"

    def record_outcome(
        self,
        task_id: str,
        attempt_id: str,
        worker_id: str,
        success: bool,
        touches: list[str] | None = None,
        files_changed: list[str] | None = None,
        failure_type: str | None = None,
    ) -> None:
        """Append a task outcome to the run history."""
        entry = {
            "ts": _now_iso(),
            "task_id": task_id,
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "success": success,
            "touches": touches or [],
            "files_changed": files_changed or [],
            "failure_type": failure_type,
        }
        with open(self._outcomes_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")

        with contextlib.suppress(Exception):
            self.recompute_hotspots()

    def get_outcomes(self, limit: int = 100) -> list[dict]:
        """Return recent task outcomes (newest first)."""
        path = self._outcomes_path()
        if not path.exists():
            return []
        lines = path.read_text().strip().split("\n")
        entries = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries

    # ------------------------------------------------------------------
    # Touch observations (actual files/scopes per task)
    # ------------------------------------------------------------------

    def _touch_obs_path(self) -> Path:
        return self._dir / "touch_observations.jsonl"

    def record_touch_observation(
        self,
        task_id: str,
        declared_touches: list[str],
        actual_files: list[str],
    ) -> None:
        """Record what files a task actually changed vs what it declared."""
        entry = {
            "ts": _now_iso(),
            "task_id": task_id,
            "declared_touches": declared_touches,
            "actual_files": actual_files,
        }
        with open(self._touch_obs_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")

    def get_touch_observations(self, limit: int = 100) -> list[dict]:
        """Return recent touch observations."""
        path = self._touch_obs_path()
        if not path.exists():
            return []
        lines = path.read_text().strip().split("\n")
        entries = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries

    # ------------------------------------------------------------------
    # Hotspots (heuristic, computed from outcomes)
    # ------------------------------------------------------------------

    def _hotspots_path(self) -> Path:
        return self._dir / "hotspots.json"

    def get_hotspots(self) -> dict[str, float]:
        """Return hotspot scores keyed by scope/file. Higher = hotter."""
        path = self._hotspots_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def recompute_hotspots(self, recent_n: int = 50) -> dict[str, float]:
        """Recompute hotspot scores from recent task outcomes.

        A scope/file is hot if it appears frequently in failed outcomes.
        Score = failure_count / total_appearances for that scope.
        """
        outcomes = self.get_outcomes(limit=recent_n)
        scope_total: dict[str, int] = {}
        scope_failures: dict[str, int] = {}

        for o in outcomes:
            scopes = set(o.get("touches", []) + o.get("files_changed", []))
            for s in scopes:
                scope_total[s] = scope_total.get(s, 0) + 1
                if not o.get("success", True):
                    scope_failures[s] = scope_failures.get(s, 0) + 1

        hotspots: dict[str, float] = {}
        for s, total in scope_total.items():
            failures = scope_failures.get(s, 0)
            if failures > 0 and total >= 2:
                hotspots[s] = round(failures / total, 3)

        self._hotspots_path().write_text(json.dumps(hotspots, indent=2))
        return hotspots

    # ------------------------------------------------------------------
    # Failure patterns (heuristic, derived from outcomes)
    # ------------------------------------------------------------------

    def _failure_patterns_path(self) -> Path:
        return self._dir / "failure_patterns.json"

    def get_failure_patterns(self) -> dict[str, int]:
        """Return failure type counts."""
        path = self._failure_patterns_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def recompute_failure_patterns(self, recent_n: int = 50) -> dict[str, int]:
        """Recompute failure pattern counts from recent outcomes."""
        outcomes = self.get_outcomes(limit=recent_n)
        patterns: dict[str, int] = {}
        for o in outcomes:
            ft = o.get("failure_type")
            if ft and not o.get("success", True):
                patterns[ft] = patterns.get(ft, 0) + 1

        self._failure_patterns_path().write_text(json.dumps(patterns, indent=2))
        return patterns

    # ------------------------------------------------------------------
    # Conflict risk scoring
    # ------------------------------------------------------------------

    def compute_conflict_risk(
        self,
        touches: list[str],
        active_touches: set[str],
    ) -> float:
        """Compute conflict risk score for a task.

        Score is 0.0 (no risk) to 1.0 (high risk) based on:
        - Overlap with active task touches
        - Whether touches map to hotspots
        """
        if not touches:
            return 0.0

        hotspots = self.get_hotspots()
        overlap = set(touches) & active_touches
        overlap_ratio = len(overlap) / len(touches) if touches else 0.0

        # Average hotspot score for this task's touches
        hotspot_scores = [hotspots.get(t, 0.0) for t in touches]
        avg_hotspot = sum(hotspot_scores) / len(hotspot_scores) if hotspot_scores else 0.0

        # Weighted combination: overlap matters more than hotspot history
        risk = 0.6 * overlap_ratio + 0.4 * avg_hotspot
        return round(min(risk, 1.0), 3)

    def check_overlap_warnings(
        self,
        touches: list[str],
        active_tasks: list[dict],
    ) -> list[str]:
        """Check for overlap warnings between a new task and active tasks.

        Returns list of warning strings for each overlapping active task.
        """
        warnings: list[str] = []
        task_touches = set(touches)
        if not task_touches:
            return warnings

        for task in active_tasks:
            active = set(task.get("touches", []))
            overlap = task_touches & active
            if overlap:
                warnings.append(
                    f"Overlaps with active task '{task.get('id', '?')}' "
                    f"on: {', '.join(sorted(overlap))}"
                )
        return warnings

"""PlannerEngine — AI-assisted task decomposition for Antfarm.

Decomposes a spec or issue into a set of tasks with dependencies, touches,
and complexity estimates. Informed by repo facts, hotspots, and touch
observations from memory.

The planner proposes tasks in the same schema used by manual `antfarm carry`.
Output is validated before carry. AI integration is optional — the planner
can also work from structured input.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProposedTask:
    """A task proposed by the planner, pending operator approval."""

    title: str
    spec: str
    touches: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    priority: int = 10
    complexity: str = "M"

    def to_carry_dict(self, task_id: str) -> dict:
        """Convert to the dict shape expected by carry()."""
        return {
            "id": task_id,
            "title": self.title,
            "spec": self.spec,
            "touches": self.touches,
            "depends_on": self.depends_on,
            "priority": self.priority,
            "complexity": self.complexity,
        }


@dataclass
class PlanResult:
    """Output of the planner."""

    tasks: list[ProposedTask]
    warnings: list[str] = field(default_factory=list)
    raw_output: str = ""


class PlannerEngine:
    """Decomposes specs into tasks with dependency and scope awareness.

    Args:
        data_dir: Path to .antfarm directory for memory access.
        agent_command: Command to run the AI agent for decomposition.
            If None, only parse_structured_plan() is available.
    """

    def __init__(
        self,
        data_dir: str = ".antfarm",
        agent_command: list[str] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._agent_command = agent_command

    def plan_from_spec(self, spec: str) -> PlanResult:
        """Decompose a text spec into proposed tasks.

        If agent_command is set, uses AI to generate the plan.
        Otherwise, attempts to parse the spec as structured JSON.
        """
        context = self._build_context()

        if self._agent_command:
            return self._plan_with_agent(spec, context)
        return self._plan_from_structured(spec)

    def plan_from_file(self, path: str) -> PlanResult:
        """Read a spec from a file and decompose it."""
        with open(path) as f:
            content = f.read()

        # If the file is JSON, try structured parsing
        if path.endswith(".json"):
            return self._plan_from_structured(content)
        return self.plan_from_spec(content)

    def parse_structured_plan(self, text: str) -> PlanResult:
        """Parse a JSON array of task objects into a PlanResult.

        Expected format:
        [
          {"title": "...", "spec": "...", "touches": [...], "depends_on": [...],
           "priority": 10, "complexity": "M"},
          ...
        ]
        """
        return self._plan_from_structured(text)

    def validate_plan(self, result: PlanResult) -> list[str]:
        """Validate a plan result. Returns list of error strings (empty = valid)."""
        errors: list[str] = []
        task_indices = set(range(len(result.tasks)))
        titles = set()

        for i, task in enumerate(result.tasks):
            if not task.title:
                errors.append(f"Task {i}: missing title")
            if not task.spec:
                errors.append(f"Task {i}: missing spec")
            if task.title in titles:
                errors.append(f"Task {i}: duplicate title '{task.title}'")
            titles.add(task.title)
            if task.complexity not in ("S", "M", "L"):
                errors.append(f"Task {i}: invalid complexity '{task.complexity}'")

            for dep in task.depends_on:
                # deps reference task IDs which aren't assigned yet,
                # so we check for index-based references like "1", "2"
                try:
                    dep_idx = int(dep) - 1  # 1-based
                    if dep_idx not in task_indices:
                        errors.append(f"Task {i}: dep '{dep}' references non-existent task")
                    if dep_idx >= i:
                        errors.append(f"Task {i}: dep '{dep}' is forward reference (cycle risk)")
                except ValueError:
                    pass  # string dep IDs are OK (resolved at carry time)

        return errors

    def generate_warnings(self, result: PlanResult) -> list[str]:
        """Generate conflict and hotspot warnings for a plan."""
        warnings: list[str] = []

        # Check for scope overlap between tasks
        for i, task_a in enumerate(result.tasks):
            touches_a = set(task_a.touches)
            if not touches_a:
                continue
            for j, task_b in enumerate(result.tasks[i + 1 :], start=i + 1):
                touches_b = set(task_b.touches)
                overlap = touches_a & touches_b
                if overlap:
                    # Check if they have a dependency relationship
                    has_dep = (
                        str(i + 1) in task_b.depends_on
                        or str(j + 1) in task_a.depends_on
                    )
                    if not has_dep:
                        warnings.append(
                            f"Tasks {i + 1} and {j + 1} both touch "
                            f"{', '.join(sorted(overlap))} — consider serializing"
                        )

        # Check hotspots
        try:
            from antfarm.core.memory import MemoryStore

            hotspots = MemoryStore(self._data_dir).get_hotspots()
            for i, task in enumerate(result.tasks):
                hot = [t for t in task.touches if hotspots.get(t, 0) > 0.3]
                if hot:
                    warnings.append(
                        f"Task {i + 1} touches hot scopes: {', '.join(hot)}"
                    )
        except Exception:
            pass

        return warnings

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        """Build context string from memory for the AI agent."""
        parts: list[str] = []
        try:
            from antfarm.core.memory import MemoryStore

            memory = MemoryStore(self._data_dir)
            facts = memory.get_facts()
            if facts:
                parts.append(f"Repo facts: {json.dumps(facts)}")
            hotspots = memory.get_hotspots()
            if hotspots:
                parts.append(f"Hotspots: {json.dumps(hotspots)}")
            observations = memory.get_touch_observations(limit=10)
            if observations:
                parts.append(
                    f"Recent touch observations: {json.dumps(observations[:5])}"
                )
        except Exception:
            pass
        return "\n".join(parts)

    def _plan_with_agent(self, spec: str, context: str) -> PlanResult:
        """Use an AI agent to decompose the spec."""
        prompt = self._build_prompt(spec, context)

        try:
            proc = subprocess.run(
                [*self._agent_command, prompt],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            raw = proc.stdout
        except subprocess.TimeoutExpired:
            logger.warning("planner agent timed out")
            return PlanResult(tasks=[], warnings=["Agent timed out"], raw_output="")
        except Exception as exc:
            logger.warning("planner agent failed: %s", exc)
            return PlanResult(tasks=[], warnings=[f"Agent error: {exc}"], raw_output="")

        result = self._parse_agent_output(raw)
        result.raw_output = raw
        result.warnings = self.generate_warnings(result)
        return result

    def _plan_from_structured(self, text: str) -> PlanResult:
        """Parse structured JSON into a plan."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return PlanResult(
                tasks=[], warnings=[f"Invalid JSON: {exc}"], raw_output=text
            )

        if isinstance(data, dict) and "tasks" in data:
            data = data["tasks"]

        if not isinstance(data, list):
            return PlanResult(
                tasks=[], warnings=["Expected JSON array of tasks"], raw_output=text
            )

        tasks: list[ProposedTask] = []
        for item in data:
            tasks.append(ProposedTask(
                title=item.get("title", ""),
                spec=item.get("spec", ""),
                touches=item.get("touches", []),
                depends_on=[str(d) for d in item.get("depends_on", [])],
                priority=item.get("priority", 10),
                complexity=item.get("complexity", "M"),
            ))

        result = PlanResult(tasks=tasks, raw_output=text)
        result.warnings = self.generate_warnings(result)
        return result

    def _build_prompt(self, spec: str, context: str) -> str:
        """Build the prompt for the AI agent."""
        return (
            "You are a task decomposer for the Antfarm orchestration system.\n\n"
            "Given a feature spec, break it into implementable tasks.\n\n"
            f"Context:\n{context}\n\n"
            f"Spec:\n{spec}\n\n"
            "Output a JSON array of tasks. Each task has:\n"
            '- "title": short description\n'
            '- "spec": detailed implementation instructions\n'
            '- "touches": list of scopes/modules affected\n'
            '- "depends_on": list of task numbers this depends on (1-based)\n'
            '- "priority": integer (lower = higher priority, default 10)\n'
            '- "complexity": "S", "M", or "L"\n\n'
            "Output ONLY the JSON array, no other text.\n"
        )

    def _parse_agent_output(self, raw: str) -> PlanResult:
        """Parse AI agent output, extracting JSON from potentially noisy output."""
        # Try to find a JSON array in the output
        import re

        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            return self._plan_from_structured(match.group(0))
        return PlanResult(
            tasks=[], warnings=["Could not find JSON array in agent output"]
        )

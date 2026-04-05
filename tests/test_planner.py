"""Tests for antfarm.core.planner — PlannerEngine."""

from __future__ import annotations

import json

import pytest

from antfarm.core.planner import PlannerEngine, PlanResult, ProposedTask


@pytest.fixture
def engine(tmp_path):
    return PlannerEngine(data_dir=str(tmp_path / ".antfarm"))


# ---------------------------------------------------------------------------
# Structured plan parsing
# ---------------------------------------------------------------------------


def test_parse_json_array(engine):
    text = json.dumps([
        {"title": "Auth middleware", "spec": "Build JWT auth", "touches": ["api", "auth"]},
        {"title": "Login endpoint", "spec": "Add POST /login", "depends_on": [1]},
    ])
    result = engine.parse_structured_plan(text)
    assert len(result.tasks) == 2
    assert result.tasks[0].title == "Auth middleware"
    assert result.tasks[0].touches == ["api", "auth"]
    assert result.tasks[1].depends_on == ["1"]


def test_parse_json_object_with_tasks_key(engine):
    text = json.dumps({"tasks": [
        {"title": "Task A", "spec": "Do A"},
    ]})
    result = engine.parse_structured_plan(text)
    assert len(result.tasks) == 1


def test_parse_invalid_json(engine):
    result = engine.parse_structured_plan("not json")
    assert result.tasks == []
    assert any("Invalid JSON" in w for w in result.warnings)


def test_parse_non_array(engine):
    result = engine.parse_structured_plan('"just a string"')
    assert result.tasks == []
    assert any("Expected JSON array" in w for w in result.warnings)


def test_parse_defaults(engine):
    text = json.dumps([{"title": "Minimal", "spec": "Do it"}])
    result = engine.parse_structured_plan(text)
    task = result.tasks[0]
    assert task.priority == 10
    assert task.complexity == "M"
    assert task.touches == []
    assert task.depends_on == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_valid_plan(engine):
    result = PlanResult(tasks=[
        ProposedTask(title="A", spec="spec A"),
        ProposedTask(title="B", spec="spec B", depends_on=["1"]),
    ])
    errors = engine.validate_plan(result)
    assert errors == []


def test_validate_missing_title(engine):
    result = PlanResult(tasks=[ProposedTask(title="", spec="spec")])
    errors = engine.validate_plan(result)
    assert any("missing title" in e for e in errors)


def test_validate_missing_spec(engine):
    result = PlanResult(tasks=[ProposedTask(title="X", spec="")])
    errors = engine.validate_plan(result)
    assert any("missing spec" in e for e in errors)


def test_validate_duplicate_titles(engine):
    result = PlanResult(tasks=[
        ProposedTask(title="Same", spec="a"),
        ProposedTask(title="Same", spec="b"),
    ])
    errors = engine.validate_plan(result)
    assert any("duplicate title" in e for e in errors)


def test_validate_invalid_complexity(engine):
    result = PlanResult(tasks=[ProposedTask(title="X", spec="x", complexity="XL")])
    errors = engine.validate_plan(result)
    assert any("invalid complexity" in e for e in errors)


def test_validate_forward_dep(engine):
    result = PlanResult(tasks=[
        ProposedTask(title="A", spec="a", depends_on=["2"]),
        ProposedTask(title="B", spec="b"),
    ])
    errors = engine.validate_plan(result)
    assert any("forward reference" in e for e in errors)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_overlap_warning(engine):
    result = PlanResult(tasks=[
        ProposedTask(title="A", spec="a", touches=["api"]),
        ProposedTask(title="B", spec="b", touches=["api"]),
    ])
    warnings = engine.generate_warnings(result)
    assert any("api" in w and "serializing" in w for w in warnings)


def test_no_overlap_warning_with_dep(engine):
    result = PlanResult(tasks=[
        ProposedTask(title="A", spec="a", touches=["api"]),
        ProposedTask(title="B", spec="b", touches=["api"], depends_on=["1"]),
    ])
    warnings = engine.generate_warnings(result)
    assert not any("serializing" in w for w in warnings)


# ---------------------------------------------------------------------------
# ProposedTask
# ---------------------------------------------------------------------------


def test_proposed_task_to_carry_dict():
    task = ProposedTask(
        title="Build auth",
        spec="JWT middleware",
        touches=["api", "auth"],
        depends_on=["task-001"],
        priority=5,
        complexity="L",
    )
    d = task.to_carry_dict("task-002")
    assert d["id"] == "task-002"
    assert d["title"] == "Build auth"
    assert d["touches"] == ["api", "auth"]
    assert d["priority"] == 5


# ---------------------------------------------------------------------------
# plan_from_spec (no agent)
# ---------------------------------------------------------------------------


def test_plan_from_spec_structured(engine):
    spec = json.dumps([
        {"title": "A", "spec": "Do A", "touches": ["db"]},
    ])
    result = engine.plan_from_spec(spec)
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "A"


# ---------------------------------------------------------------------------
# plan_from_file
# ---------------------------------------------------------------------------


def test_plan_from_json_file(engine, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps([
        {"title": "X", "spec": "Do X"},
        {"title": "Y", "spec": "Do Y", "depends_on": [1]},
    ]))
    result = engine.plan_from_file(str(f))
    assert len(result.tasks) == 2


def test_plan_from_text_file(engine, tmp_path):
    f = tmp_path / "plan.txt"
    f.write_text(json.dumps([{"title": "Z", "spec": "Do Z"}]))
    result = engine.plan_from_file(str(f))
    assert len(result.tasks) == 1

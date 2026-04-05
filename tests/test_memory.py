"""Tests for antfarm.core.memory — MemoryStore."""

from __future__ import annotations

import pytest

from antfarm.core.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / ".antfarm"))


# ---------------------------------------------------------------------------
# Repo facts
# ---------------------------------------------------------------------------


def test_get_facts_empty(store):
    assert store.get_facts() == {}


def test_set_and_get_fact(store):
    store.set_fact("language", "python")
    facts = store.get_facts()
    assert facts["language"] == "python"


def test_set_fact_overwrites(store):
    store.set_fact("test_command", "pytest")
    store.set_fact("test_command", "pytest -x")
    assert store.get_facts()["test_command"] == "pytest -x"


def test_detect_facts_python(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    detected = store.detect_facts(str(repo))
    assert detected["language"] == "python"
    assert "pytest" in detected["test_command"]

    facts = store.get_facts()
    assert facts["language"] == "python"


def test_detect_facts_does_not_overwrite_operator(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("")

    store.set_fact("language", "custom")
    store.detect_facts(str(repo))
    assert store.get_facts()["language"] == "custom"


def test_detect_facts_javascript(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}")

    detected = store.detect_facts(str(repo))
    assert detected["language"] == "javascript"


def test_detect_facts_rust(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Cargo.toml").write_text("")

    detected = store.detect_facts(str(repo))
    assert detected["language"] == "rust"


def test_detect_facts_go(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("")

    detected = store.detect_facts(str(repo))
    assert detected["language"] == "go"


# ---------------------------------------------------------------------------
# Task outcomes
# ---------------------------------------------------------------------------


def test_record_and_get_outcomes(store):
    store.record_outcome("t1", "a1", "w1", success=True, touches=["api"])
    store.record_outcome("t2", "a2", "w1", success=False, failure_type="test_failure")

    outcomes = store.get_outcomes()
    assert len(outcomes) == 2
    assert outcomes[0]["task_id"] == "t2"  # newest first
    assert outcomes[1]["task_id"] == "t1"


def test_get_outcomes_empty(store):
    assert store.get_outcomes() == []


def test_get_outcomes_limit(store):
    for i in range(10):
        store.record_outcome(f"t{i}", f"a{i}", "w1", success=True)
    assert len(store.get_outcomes(limit=3)) == 3


# ---------------------------------------------------------------------------
# Touch observations
# ---------------------------------------------------------------------------


def test_record_touch_observation(store):
    store.record_touch_observation("t1", ["api", "auth"], ["src/api.py", "src/auth.py"])

    obs = store.get_touch_observations()
    assert len(obs) == 1
    assert obs[0]["task_id"] == "t1"
    assert obs[0]["declared_touches"] == ["api", "auth"]
    assert "src/api.py" in obs[0]["actual_files"]


def test_get_touch_observations_empty(store):
    assert store.get_touch_observations() == []


# ---------------------------------------------------------------------------
# Hotspots
# ---------------------------------------------------------------------------


def test_get_hotspots_empty(store):
    assert store.get_hotspots() == {}


def test_recompute_hotspots(store):
    # Record outcomes: "api" appears in 4 outcomes, fails in 2
    store.record_outcome("t1", "a1", "w1", success=True, touches=["api"])
    store.record_outcome("t2", "a2", "w1", success=False, touches=["api"])
    store.record_outcome("t3", "a3", "w1", success=True, touches=["api"])
    store.record_outcome("t4", "a4", "w1", success=False, touches=["api"])

    hotspots = store.recompute_hotspots()
    assert "api" in hotspots
    assert hotspots["api"] == 0.5  # 2 failures / 4 total

    # Verify persisted
    assert store.get_hotspots()["api"] == 0.5


def test_recompute_hotspots_no_failures(store):
    store.record_outcome("t1", "a1", "w1", success=True, touches=["api"])
    store.record_outcome("t2", "a2", "w1", success=True, touches=["api"])

    hotspots = store.recompute_hotspots()
    assert hotspots == {}  # No failures = no hotspots


def test_recompute_hotspots_single_appearance_excluded(store):
    store.record_outcome("t1", "a1", "w1", success=False, touches=["rare"])

    hotspots = store.recompute_hotspots()
    assert "rare" not in hotspots  # Need >= 2 appearances


# ---------------------------------------------------------------------------
# Failure patterns
# ---------------------------------------------------------------------------


def test_recompute_failure_patterns(store):
    store.record_outcome("t1", "a1", "w1", success=False, failure_type="test_failure")
    store.record_outcome("t2", "a2", "w1", success=False, failure_type="test_failure")
    store.record_outcome("t3", "a3", "w1", success=False, failure_type="lint_failure")

    patterns = store.recompute_failure_patterns()
    assert patterns["test_failure"] == 2
    assert patterns["lint_failure"] == 1


# ---------------------------------------------------------------------------
# Conflict risk
# ---------------------------------------------------------------------------


def test_conflict_risk_no_overlap(store):
    risk = store.compute_conflict_risk(["api"], set())
    assert risk == 0.0


def test_conflict_risk_full_overlap(store):
    risk = store.compute_conflict_risk(["api"], {"api"})
    assert risk >= 0.5


def test_conflict_risk_with_hotspot(store):
    # Create a hotspot
    store.record_outcome("t1", "a1", "w1", success=False, touches=["api"])
    store.record_outcome("t2", "a2", "w1", success=False, touches=["api"])
    store.recompute_hotspots()

    risk = store.compute_conflict_risk(["api"], set())
    assert risk > 0.0  # Hotspot contributes to risk even without overlap


def test_conflict_risk_empty_touches(store):
    assert store.compute_conflict_risk([], {"api"}) == 0.0


# ---------------------------------------------------------------------------
# Overlap warnings
# ---------------------------------------------------------------------------


def test_check_overlap_warnings_no_overlap(store):
    active = [{"id": "t1", "touches": ["db"]}]
    warnings = store.check_overlap_warnings(["api"], active)
    assert warnings == []


def test_check_overlap_warnings_with_overlap(store):
    active = [{"id": "t1", "touches": ["api", "auth"]}]
    warnings = store.check_overlap_warnings(["api"], active)
    assert len(warnings) == 1
    assert "t1" in warnings[0]
    assert "api" in warnings[0]


def test_check_overlap_warnings_multiple(store):
    active = [
        {"id": "t1", "touches": ["api"]},
        {"id": "t2", "touches": ["api", "db"]},
    ]
    warnings = store.check_overlap_warnings(["api", "db"], active)
    assert len(warnings) == 2

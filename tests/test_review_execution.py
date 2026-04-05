"""Tests for v0.5.3 review execution.

Covers: store_review_verdict backend/API, Soldier review orchestration,
worker verdict parsing, and full e2e carry→build→review→merge.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.colony_client import ColonyClient
from antfarm.core.models import Attempt, AttemptStatus, ReviewVerdict
from antfarm.core.serve import get_app
from antfarm.core.soldier import MergeResult, Soldier
from antfarm.core.worker import _extract_branch_from_spec, _parse_review_verdict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def _configure_git(repo):
    _git(["git", "config", "user.email", "test@antfarm.test"], cwd=repo)
    _git(["git", "config", "user.name", "Antfarm Test"], cwd=repo)


def _commit_file(repo, filename, content, message):
    with open(f"{repo}/{filename}", "w") as f:
        f.write(content)
    _git(["git", "add", filename], cwd=repo)
    _git(["git", "commit", "-m", message], cwd=repo)


def _make_verdict(verdict="pass", sha="abc123", provider="test"):
    return ReviewVerdict(
        provider=provider,
        verdict=verdict,
        summary="looks good",
        findings=[],
        reviewed_commit_sha=sha,
    ).to_dict()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path):
    return FileBackend(root=str(tmp_path / ".antfarm"))


@pytest.fixture
def tc(backend):
    app = get_app(backend=backend)
    return TestClient(app, raise_server_exceptions=False)


def _carry_task(tc, task_id="task-001", **kw):
    """Carry a task via API."""
    payload = {
        "id": task_id,
        "title": kw.get("title", f"Task {task_id}"),
        "spec": kw.get("spec", "do the thing"),
        "depends_on": kw.get("depends_on", []),
        "priority": kw.get("priority", 10),
    }
    if "capabilities_required" in kw:
        payload["capabilities_required"] = kw["capabilities_required"]
    return tc.post("/tasks", json=payload)


def _forage_and_harvest(tc, task_id, worker_id="w-1"):
    """Forage a task and harvest it."""
    r = tc.post("/tasks/pull", json={"worker_id": worker_id})
    assert r.status_code == 200
    task = r.json()
    attempt_id = task["current_attempt"]
    r = tc.post(
        f"/tasks/{task_id}/harvest",
        json={"attempt_id": attempt_id, "pr": "pr-1", "branch": "feat/x"},
    )
    assert r.status_code == 200
    return attempt_id


@pytest.fixture
def soldier_env(tmp_path):
    """Full environment for soldier review tests."""
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"

    _git(["git", "init", "--bare", str(origin)], cwd=str(tmp_path))
    _git(["git", "clone", str(origin), str(clone)], cwd=str(tmp_path))
    _configure_git(str(clone))

    _commit_file(str(clone), "README.md", "test\n", "init")
    _git(["git", "push", "origin", "HEAD:dev"], cwd=str(clone))
    _git(["git", "fetch", "origin"], cwd=str(clone))
    _git(["git", "checkout", "-b", "dev", "origin/dev"], cwd=str(clone), check=False)
    _git(["git", "checkout", "dev"], cwd=str(clone), check=False)
    _git(
        ["git", "branch", "--set-upstream-to=origin/dev", "dev"],
        cwd=str(clone), check=False,
    )

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    http_client = TestClient(app, raise_server_exceptions=True)
    cc = ColonyClient("http://testserver", client=http_client)

    soldier = Soldier(
        colony_url="http://testserver",
        repo_path=str(clone),
        integration_branch="dev",
        test_command=["true"],
        poll_interval=0.0,
        require_review=True,
        client=http_client,
    )

    yield {
        "soldier": soldier,
        "cc": cc,
        "backend": backend,
        "repo": str(clone),
        "http_client": http_client,
    }


def _carry_and_harvest_git(cc, repo, task_id, branch_name, **kw):
    """Carry, forage, commit, push, harvest — with real git."""
    worker_id = f"worker-{task_id}"
    cc.register_worker(
        worker_id=worker_id, node_id="n1", agent_type="generic", workspace_root="/tmp/ws",
    )
    cc._client.post("/tasks", json={
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "do the thing",
        "depends_on": kw.get("depends_on", []),
        "priority": kw.get("priority", 10),
    }).raise_for_status()

    task = cc.forage(worker_id)
    assert task is not None
    attempt_id = task["current_attempt"]

    _git(["git", "checkout", "-b", branch_name, "origin/dev"], cwd=repo)
    _commit_file(repo, f"{task_id}.txt", "change\n", f"work for {task_id}")
    _git(["git", "push", "origin", branch_name], cwd=repo)
    _git(["git", "checkout", "dev"], cwd=repo)

    cc.harvest(task_id=task_id, attempt_id=attempt_id, pr="pr", branch=branch_name)
    return cc.get_task(task_id)


# ===========================================================================
# test_file_backend.py equivalents
# ===========================================================================


class TestFileBackendReviewVerdict:
    def test_store_review_verdict_on_done_task(self, backend, tc):
        _carry_task(tc)
        tc.post("/workers/register", json={
            "worker_id": "w-1", "node_id": "n1",
            "agent_type": "generic", "workspace_root": "/tmp",
        })
        attempt_id = _forage_and_harvest(tc, "task-001")
        verdict = _make_verdict()

        backend.store_review_verdict("task-001", attempt_id, verdict)

        task = backend.get_task("task-001")
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                assert a["review_verdict"] == verdict
                return
        pytest.fail("attempt not found")

    def test_store_review_verdict_wrong_attempt_raises(self, backend, tc):
        _carry_task(tc)
        tc.post("/workers/register", json={
            "worker_id": "w-1", "node_id": "n1",
            "agent_type": "generic", "workspace_root": "/tmp",
        })
        _forage_and_harvest(tc, "task-001")

        with pytest.raises(ValueError, match="not the current attempt"):
            backend.store_review_verdict("task-001", "wrong-id", _make_verdict())

    def test_store_review_verdict_not_done_raises(self, backend, tc):
        _carry_task(tc)
        with pytest.raises(FileNotFoundError):
            backend.store_review_verdict("task-001", "att-1", _make_verdict())


# ===========================================================================
# test_serve.py equivalents
# ===========================================================================


class TestServeReviewVerdict:
    def test_review_verdict_endpoint(self, tc):
        _carry_task(tc)
        tc.post("/workers/register", json={
            "worker_id": "w-1", "node_id": "n1",
            "agent_type": "generic", "workspace_root": "/tmp",
        })
        attempt_id = _forage_and_harvest(tc, "task-001")
        verdict = _make_verdict()

        r = tc.post(
            "/tasks/task-001/review-verdict",
            json={"attempt_id": attempt_id, "verdict": verdict},
        )
        assert r.status_code == 200

        task = tc.get("/tasks/task-001").json()
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                assert a["review_verdict"] == verdict

    def test_review_verdict_wrong_attempt_409(self, tc):
        _carry_task(tc)
        tc.post("/workers/register", json={
            "worker_id": "w-1", "node_id": "n1",
            "agent_type": "generic", "workspace_root": "/tmp",
        })
        _forage_and_harvest(tc, "task-001")

        r = tc.post(
            "/tasks/task-001/review-verdict",
            json={"attempt_id": "wrong", "verdict": _make_verdict()},
        )
        assert r.status_code == 409

    def test_review_verdict_not_found_404(self, tc):
        r = tc.post(
            "/tasks/nonexistent/review-verdict",
            json={"attempt_id": "att", "verdict": _make_verdict()},
        )
        assert r.status_code == 404


# ===========================================================================
# test_soldier.py equivalents
# ===========================================================================


class TestSoldierReview:
    def test_process_done_tasks_creates_review_task(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        _carry_and_harvest_git(cc, repo, "task-p1", "feat/task-p1")

        created = soldier.process_done_tasks()
        assert "review-task-p1" in created

        review = cc.get_task("review-task-p1")
        assert review is not None
        assert review["status"] == "ready"

    def test_process_done_tasks_skips_when_verdict_exists(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-p2", "feat/task-p2")

        # Store verdict
        cc.store_review_verdict("task-p2", task["current_attempt"], _make_verdict())

        created = soldier.process_done_tasks()
        assert "review-task-p2" not in created

    def test_process_done_tasks_skips_review_tasks(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        _carry_and_harvest_git(cc, repo, "task-p3", "feat/task-p3")

        # First call creates review task
        soldier.process_done_tasks()
        # Forage and harvest the review task so it's "done"
        reviewer = "reviewer-p3"
        cc.register_worker(
            worker_id=reviewer, node_id="n1",
            agent_type="generic", workspace_root="/tmp/ws",
            capabilities=["review"],
        )
        rt = cc.forage(reviewer)
        if rt:
            cc.harvest(rt["id"], rt["current_attempt"], pr="", branch="rb")

        # Second call should NOT create review-review-task-p3
        created = soldier.process_done_tasks()
        assert not any(rid.startswith("review-review-") for rid in created)

    def test_merge_queue_excludes_without_verdict(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        _carry_and_harvest_git(cc, repo, "task-q1", "feat/task-q1")

        queue = soldier.get_merge_queue()
        assert not any(t["id"] == "task-q1" for t in queue)

    def test_merge_queue_includes_with_passing_verdict(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-q2", "feat/task-q2")
        cc.store_review_verdict("task-q2", task["current_attempt"], _make_verdict())

        queue = soldier.get_merge_queue()
        assert any(t["id"] == "task-q2" for t in queue)

    def test_merge_queue_excludes_needs_changes_verdict(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-q3", "feat/task-q3")
        cc.store_review_verdict(
            "task-q3", task["current_attempt"],
            _make_verdict(verdict="needs_changes"),
        )

        queue = soldier.get_merge_queue()
        assert not any(t["id"] == "task-q3" for t in queue)

    def test_merge_queue_excludes_stale_verdict(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-q4", "feat/task-q4")

        # Store verdict with passing but add artifact with mismatched SHA
        verdict = _make_verdict(sha="old_sha")
        attempt_id = task["current_attempt"]
        cc.store_review_verdict("task-q4", attempt_id, verdict)

        # Store artifact with different head SHA
        cc._client.post("/tasks/task-q4/harvest", json={
            "attempt_id": attempt_id,
            "pr": "pr",
            "branch": "feat/task-q4",
            "artifact": {
                "head_commit_sha": "completely_different_sha",
                "target_branch_sha_at_harvest": "xxx",
            },
        })
        # This will fail because already harvested — that's OK, the point is
        # check_review_verdict handles mismatched SHAs
        # Instead, test check_review_verdict directly
        task_data = cc.get_task("task-q4")
        # Manually add artifact to attempt
        for a in task_data["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["artifact"] = {
                    "head_commit_sha": "completely_different_sha",
                    "target_branch_sha_at_harvest": "xxx",
                }
        passed, reason = soldier.check_review_verdict(task_data)
        assert not passed
        assert "stale" in reason

    def test_create_review_task_includes_review_pack(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]

        # Carry and harvest with artifact
        worker_id = "worker-rp"
        cc.register_worker(
            worker_id=worker_id, node_id="n1",
            agent_type="generic", workspace_root="/tmp/ws",
        )
        cc._client.post("/tasks", json={
            "id": "task-rp", "title": "Task RP", "spec": "spec",
            "depends_on": [],
        }).raise_for_status()
        task = cc.forage(worker_id)
        attempt_id = task["current_attempt"]

        _git(["git", "checkout", "-b", "feat/task-rp", "origin/dev"], cwd=repo)
        _commit_file(repo, "rp.txt", "change\n", "work for rp")
        _git(["git", "push", "origin", "feat/task-rp"], cwd=repo)
        _git(["git", "checkout", "dev"], cwd=repo)

        artifact = {
            "task_id": "task-rp",
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "branch": "feat/task-rp",
            "pr_url": None,
            "base_commit_sha": "abc",
            "head_commit_sha": "def",
            "target_branch": "dev",
            "target_branch_sha_at_harvest": "aaa",
            "files_changed": ["rp.txt"],
            "lines_added": 1,
            "lines_removed": 0,
            "tests_ran": True,
            "tests_passed": True,
            "merge_readiness": "needs_review",
        }
        cc.harvest(
            task_id="task-rp", attempt_id=attempt_id,
            pr="pr", branch="feat/task-rp", artifact=artifact,
        )

        task_data = cc.get_task("task-rp")
        review_id = soldier.create_review_task(task_data)
        assert review_id == "review-task-rp"

        review_task = cc.get_task("review-task-rp")
        assert "Review Pack" in review_task["spec"]
        assert "rp.txt" in review_task["spec"]

    def test_create_review_task_idempotent(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-idem", "feat/task-idem")

        id1 = soldier.create_review_task(task)
        assert id1 == "review-task-idem"

        # Second call returns None (idempotent)
        id2 = soldier.create_review_task(task)
        assert id2 is None

    def test_run_once_creates_reviews_then_merges(self, soldier_env):
        cc, soldier, repo = soldier_env["cc"], soldier_env["soldier"], soldier_env["repo"]
        task = _carry_and_harvest_git(cc, repo, "task-rm", "feat/task-rm")

        # run_once with require_review: creates review tasks, no merge
        results1 = soldier.run_once()
        assert len(results1) == 0  # nothing in merge queue (no verdict)

        # Check review task was created by process_done_tasks
        review = cc.get_task("review-task-rm")
        assert review is not None

        # Store verdict
        cc.store_review_verdict(
            "task-rm", task["current_attempt"], _make_verdict()
        )

        # Now merge queue has the task
        results2 = soldier.run_once()
        assert results2 == [("task-rm", MergeResult.MERGED)]


# ===========================================================================
# test_worker.py equivalents
# ===========================================================================


class TestWorkerReviewParsing:
    def test_parse_review_verdict_valid(self):
        output = (
            'Some text\n[REVIEW_VERDICT]'
            '{"provider":"test","verdict":"pass","summary":"good","findings":[]}'
            '[/REVIEW_VERDICT]\nMore text'
        )
        result = _parse_review_verdict(output)
        assert result is not None
        assert result["verdict"] == "pass"
        assert result["provider"] == "test"

    def test_parse_review_verdict_missing_tags(self):
        assert _parse_review_verdict("no tags here") is None

    def test_parse_review_verdict_invalid_json(self):
        output = "[REVIEW_VERDICT]not json[/REVIEW_VERDICT]"
        assert _parse_review_verdict(output) is None

    def test_parse_review_verdict_missing_required_fields(self):
        output = '[REVIEW_VERDICT]{"provider":"test"}[/REVIEW_VERDICT]'
        assert _parse_review_verdict(output) is None

    def test_parse_review_verdict_invalid_verdict_value(self):
        output = (
            '[REVIEW_VERDICT]'
            '{"provider":"test","verdict":"maybe","summary":"hmm"}'
            '[/REVIEW_VERDICT]'
        )
        assert _parse_review_verdict(output) is None

    def test_extract_branch_from_spec(self):
        spec = "Review task t1\n\nBranch: feat/task-001\nPR: pr-1\n"
        assert _extract_branch_from_spec(spec) == "feat/task-001"

    def test_extract_branch_from_spec_missing(self):
        assert _extract_branch_from_spec("no branch here") is None


# ===========================================================================
# test_models.py equivalent
# ===========================================================================


def test_attempt_roundtrip_with_review_verdict():
    verdict = _make_verdict()
    a = Attempt(
        attempt_id="att-1",
        worker_id="w-1",
        status=AttemptStatus.DONE,
        branch="feat/x",
        pr="pr-1",
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T01:00:00",
        review_verdict=verdict,
    )
    d = a.to_dict()
    assert d["review_verdict"] == verdict

    a2 = Attempt.from_dict(d)
    assert a2.review_verdict == verdict


def test_attempt_roundtrip_without_review_verdict():
    a = Attempt(
        attempt_id="att-1",
        worker_id="w-1",
        status=AttemptStatus.DONE,
        branch="feat/x",
        pr="pr-1",
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T01:00:00",
    )
    d = a.to_dict()
    assert "review_verdict" not in d

    a2 = Attempt.from_dict(d)
    assert a2.review_verdict is None


# ===========================================================================
# test_e2e — full autonomous loop
# ===========================================================================


def test_e2e_carry_build_review_merge(soldier_env):
    """Full e2e: carry → build → review task created → verdict stored → merge."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["cc"]
    repo = soldier_env["repo"]

    # 1. Carry and harvest the build task
    task = _carry_and_harvest_git(cc, repo, "task-e2e", "feat/task-e2e")
    attempt_id = task["current_attempt"]

    # 2. Soldier creates review task via process_done_tasks
    soldier.process_done_tasks()
    review = cc.get_task("review-task-e2e")
    assert review is not None

    # 3. Reviewer forages review task
    reviewer_id = "reviewer-e2e"
    cc.register_worker(
        worker_id=reviewer_id, node_id="n1",
        agent_type="claude-code", workspace_root="/tmp/ws",
        capabilities=["review"],
    )
    review_task = cc.forage(reviewer_id)
    assert review_task is not None
    assert review_task["id"] == "review-task-e2e"
    review_attempt_id = review_task["current_attempt"]

    # 4. Reviewer harvests review task with verdict as artifact
    verdict = _make_verdict()
    cc.harvest(
        task_id="review-task-e2e",
        attempt_id=review_attempt_id,
        pr="", branch="review-branch",
        artifact=verdict,
    )

    # 5. Soldier extracts verdict from review task and stores on original
    results = soldier.run_once_with_review()
    assert results == [("task-e2e", MergeResult.MERGED)]

    # 6. Verify: dev has the change, task is merged
    log = _git(["git", "log", "--oneline", "dev"], cwd=repo)
    assert "work for task-e2e" in log.stdout

    final = cc.get_task("task-e2e")
    for a in final["attempts"]:
        if a["attempt_id"] == attempt_id:
            assert a["status"] == "merged"
            assert a["review_verdict"] == verdict
            return
    pytest.fail("attempt not found")

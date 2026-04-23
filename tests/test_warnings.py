"""Tests for antfarm.core.warnings — pure warning-detection helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from antfarm.core.warnings import (
    _count_awaiting_review,
    detect_no_reviewer_capacity,
    detect_review_queue_saturated,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _done_unreviewed(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "attempts": [{"attempt_id": "att-1", "status": "done"}],
        "current_attempt": "att-1",
        "capabilities_required": [],
    }


def _done_merged(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "attempts": [{"attempt_id": "att-1", "status": "merged"}],
        "current_attempt": "att-1",
        "capabilities_required": [],
    }


def _done_cancelled(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "attempts": [{"attempt_id": "att-1", "status": "done"}],
        "current_attempt": "att-1",
        "cancelled_at": datetime.now(UTC).isoformat(),
        "capabilities_required": [],
    }


def _done_passed_review(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "attempts": [
            {
                "attempt_id": "att-1",
                "status": "done",
                "review_verdict": {"verdict": "pass"},
            }
        ],
        "current_attempt": "att-1",
        "capabilities_required": [],
    }


def _done_infra(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "attempts": [{"attempt_id": "att-1", "status": "done"}],
        "current_attempt": "att-1",
        "capabilities_required": ["review"],
    }


def _ready_review(task_id: str = "review-1") -> dict:
    return {
        "id": task_id,
        "status": "ready",
        "attempts": [],
        "current_attempt": None,
        "capabilities_required": ["review"],
    }


# ---------------------------------------------------------------------------
# _count_awaiting_review
# ---------------------------------------------------------------------------


class TestCountAwaitingReview:
    def test_counts_done_unreviewed(self):
        assert _count_awaiting_review([_done_unreviewed("t1"), _done_unreviewed("t2")]) == 2

    def test_excludes_merged(self):
        assert _count_awaiting_review([_done_merged("t1")]) == 0

    def test_excludes_cancelled(self):
        assert _count_awaiting_review([_done_cancelled("t1")]) == 0

    def test_excludes_passed_review(self):
        assert _count_awaiting_review([_done_passed_review("t1")]) == 0

    def test_excludes_infra(self):
        assert _count_awaiting_review([_done_infra("t1")]) == 0

    def test_counts_ready_review_tasks(self):
        assert _count_awaiting_review([_ready_review("review-1")]) == 1

    def test_sum_of_done_and_ready(self):
        tasks = [
            _done_unreviewed("t1"),
            _done_unreviewed("t2"),
            _ready_review("review-1"),
        ]
        assert _count_awaiting_review(tasks) == 3


# ---------------------------------------------------------------------------
# detect_review_queue_saturated
# ---------------------------------------------------------------------------


class TestDetectReviewQueueSaturated:
    def test_below_threshold_returns_none(self):
        tasks = [_done_unreviewed(f"t{i}") for i in range(4)]  # threshold is 4
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=2,
            awaiting_first_seen_at=None,
            now=datetime.now(UTC),
        )
        assert result is None

    def test_above_threshold_no_sidecar_returns_none(self):
        tasks = [_done_unreviewed(f"t{i}") for i in range(5)]
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=2,
            awaiting_first_seen_at=None,
            now=datetime.now(UTC),
        )
        assert result is None  # Just-observed; caller should persist now and retry.

    def test_above_threshold_within_dwell_returns_none(self):
        tasks = [_done_unreviewed(f"t{i}") for i in range(5)]
        now = datetime.now(UTC)
        first_seen = (now - timedelta(seconds=30)).isoformat()
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=2,
            awaiting_first_seen_at=first_seen,
            now=now,
        )
        assert result is None

    def test_above_threshold_past_dwell_fires(self):
        tasks = [_done_unreviewed(f"t{i}") for i in range(5)]
        now = datetime.now(UTC)
        first_seen = (now - timedelta(seconds=130)).isoformat()
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=2,
            awaiting_first_seen_at=first_seen,
            now=now,
        )
        assert result is not None
        assert result["code"] == "review_queue_saturated"
        assert result["count"] == 5
        assert "Increase --max-reviewers" in result["hint"]
        assert "#347" in result["hint"]

    def test_threshold_scales_with_max_reviewers(self):
        """max_reviewers=3 → threshold=6; 6 awaiting is NOT saturation."""
        tasks = [_done_unreviewed(f"t{i}") for i in range(6)]
        now = datetime.now(UTC)
        first_seen = (now - timedelta(seconds=600)).isoformat()
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=3,
            awaiting_first_seen_at=first_seen,
            now=now,
        )
        assert result is None  # 6 !> 6

        # 7 does fire.
        tasks.append(_done_unreviewed("t7"))
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=3,
            awaiting_first_seen_at=first_seen,
            now=now,
        )
        assert result is not None
        assert result["count"] == 7

    def test_malformed_first_seen_returns_none(self):
        tasks = [_done_unreviewed(f"t{i}") for i in range(5)]
        result = detect_review_queue_saturated(
            tasks=tasks,
            max_reviewers=2,
            awaiting_first_seen_at="not-an-iso-string",
            now=datetime.now(UTC),
        )
        assert result is None


# ---------------------------------------------------------------------------
# detect_no_reviewer_capacity — regression smoke tests
# ---------------------------------------------------------------------------


class TestDetectNoReviewerCapacity:
    def test_no_ready_review_returns_none(self):
        assert detect_no_reviewer_capacity([], []) is None

    def test_ready_review_without_reviewer_fires(self):
        tasks = [_ready_review("review-1")]
        workers: list[dict] = []
        result = detect_no_reviewer_capacity(tasks, workers)
        assert result is not None
        assert result["code"] == "no_reviewer_capacity"

    def test_ready_review_with_reviewer_worker_silent(self):
        tasks = [_ready_review("review-1")]
        workers = [{"worker_id": "w1", "capabilities": ["review"]}]
        assert detect_no_reviewer_capacity(tasks, workers) is None

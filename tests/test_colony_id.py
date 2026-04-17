"""Tests for the persisted-UUID colony identity (#238)."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import uuid

from antfarm.core.process_manager import colony_hash, colony_id, colony_session_hash


def _mp_colony_id_worker(data_dir: str, queue: multiprocessing.Queue) -> None:
    """Top-level worker for multiprocess concurrency test.

    Must be defined at module scope so 'spawn' start method can pickle it.
    """
    queue.put(colony_id(data_dir))


def test_colony_id_generated_on_first_call(tmp_path):
    """Empty data_dir: first call generates a UUID and writes config.json."""
    data_dir = str(tmp_path)
    cid = colony_id(data_dir)
    # Must parse as a UUID (no exception = pass).
    uuid.UUID(cid)

    config_path = os.path.join(data_dir, "config.json")
    assert os.path.exists(config_path)
    with open(config_path) as f:
        cfg = json.load(f)
    assert cfg["colony_id"] == cid


def test_colony_id_persisted_across_calls(tmp_path):
    """Two calls against the same data_dir return the same id."""
    data_dir = str(tmp_path)
    first = colony_id(data_dir)
    second = colony_id(data_dir)
    assert first == second


def test_colony_id_not_regenerated_when_present(tmp_path):
    """Pre-seeded config.json::colony_id is returned verbatim."""
    data_dir = str(tmp_path)
    seeded = str(uuid.uuid4())
    with open(os.path.join(data_dir, "config.json"), "w") as f:
        json.dump({"colony_id": seeded}, f)

    assert colony_id(data_dir) == seeded


def test_colony_id_preserves_existing_config_keys(tmp_path):
    """Generating a new id must not clobber unrelated config keys."""
    data_dir = str(tmp_path)
    with open(os.path.join(data_dir, "config.json"), "w") as f:
        json.dump({"repo_path": "/foo"}, f)

    cid = colony_id(data_dir)

    with open(os.path.join(data_dir, "config.json")) as f:
        cfg = json.load(f)
    assert cfg["repo_path"] == "/foo"
    assert cfg["colony_id"] == cid


def test_colony_id_fallback_when_data_dir_missing(tmp_path):
    """Missing data_dir: return a non-empty sentinel, create no file."""
    data_dir = str(tmp_path / "does-not-exist")
    result = colony_id(data_dir)

    assert result
    assert result.startswith("legacy:")
    assert not os.path.exists(data_dir)


def test_colony_id_accepts_non_uuid_strings(tmp_path):
    """Any non-empty string in config.json::colony_id is returned verbatim."""
    data_dir = str(tmp_path)
    with open(os.path.join(data_dir, "config.json"), "w") as f:
        json.dump({"colony_id": "custom-name"}, f)

    assert colony_id(data_dir) == "custom-name"


def test_colony_id_concurrent_writers(tmp_path):
    """Racing first-call generations converge on a single id."""
    data_dir = str(tmp_path)
    results: list[str] = []
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        results.append(colony_id(data_dir))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1, results
    with open(os.path.join(data_dir, "config.json")) as f:
        cfg = json.load(f)
    assert cfg["colony_id"] == results[0]


def test_colony_id_multiprocess_concurrent_writers(tmp_path):
    """Racing first-call generations across processes converge on a single id.

    The thread-based test above exercises the in-process ``threading.Lock``
    path. The actual concurrency contract of ``colony_id`` is ``fcntl.flock``,
    which is a cross-process primitive. This test exercises that contract by
    spawning real OS processes that race through generation.
    """
    data_dir = str(tmp_path)

    # Use get_context('spawn') explicitly so the test is portable (macOS
    # defaults to 'spawn' already; Linux defaults to 'fork'). Using an
    # explicit context avoids mutating global multiprocessing state.
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [ctx.Process(target=_mp_colony_id_worker, args=(data_dir, queue)) for _ in range(5)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    results = [queue.get(timeout=5) for _ in processes]
    assert len(results) == 5
    assert all(results), results
    assert len(set(results)) == 1, results

    with open(os.path.join(data_dir, "config.json")) as f:
        cfg = json.load(f)
    assert cfg["colony_id"] == results[0]


def test_colony_session_hash_derives_from_id(tmp_path):
    """colony_session_hash == colony_hash(colony_id(data_dir)) and 8 hex chars."""
    data_dir = str(tmp_path)
    h = colony_session_hash(data_dir)

    assert h == colony_hash(colony_id(data_dir))
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_colony_session_hash_stable_across_realpath_change(tmp_path):
    """Same colony_id in two different paths yields the same session hash.

    This is the property the legacy ``colony_hash(data_dir)`` lacked:
    moving the directory previously produced a new prefix, orphaning all
    running tmux sessions. Pinning identity to a persisted UUID means two
    colonies at different paths with the same ``colony_id`` produce the
    same hash — i.e., hash tracks identity, not location.
    """
    shared_id = str(uuid.uuid4())

    dir_a = tmp_path / "a"
    dir_a.mkdir()
    with open(dir_a / "config.json", "w") as f:
        json.dump({"colony_id": shared_id}, f)

    dir_b = tmp_path / "b"
    dir_b.mkdir()
    with open(dir_b / "config.json", "w") as f:
        json.dump({"colony_id": shared_id}, f)

    assert colony_session_hash(str(dir_a)) == colony_session_hash(str(dir_b))

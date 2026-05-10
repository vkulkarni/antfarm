"""Tests for antfarm.adapters.claude_code.hooks.stop.sh.

Verifies the Stop hook correctly reads the transcript path from the JSON event
piped on stdin (Claude Code's hook protocol) and POSTs a usage event to the
colony. Also verifies the env-var fallback and the best-effort failure paths.

These tests require `jq`, `tac`, `curl` to be on PATH. macOS dev boxes do not
ship `tac`, so the suite skips locally there; CI runs Ubuntu and exercises
all of them.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from antfarm.core.hook_setup import stop_hook_path

REQUIRED_TOOLS = ("jq", "tac", "curl")


def _missing_tools() -> list[str]:
    return [t for t in REQUIRED_TOOLS if shutil.which(t) is None]


pytestmark = pytest.mark.skipif(
    bool(_missing_tools()),
    reason=f"stop.sh tests require {REQUIRED_TOOLS} on PATH; missing: {_missing_tools()}",
)


class _RecordingHandler(BaseHTTPRequestHandler):
    """Capture POST path + body into the server's `recorded` list."""

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.recorded.append((self.path, body))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args, **kwargs):  # silence test output
        return


@pytest.fixture
def mock_colony():
    """Run an in-process HTTP server that records POSTs."""
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), _RecordingHandler)
    server.recorded = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield server, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_fake_transcript(path) -> None:
    """Write a JSONL transcript with one assistant line carrying a usage block."""
    line = {
        "message": {
            "model": "claude-sonnet-4-7",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        }
    }
    path.write_text(json.dumps(line) + "\n")


def _run_hook(stdin_bytes: bytes, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """Invoke the bundled stop.sh with the given stdin and env overlay."""
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(env_extra)
    return subprocess.run(
        [str(stop_hook_path())],
        input=stdin_bytes,
        env=env,
        capture_output=True,
        timeout=10,
    )


def test_stop_hook_reads_transcript_path_from_stdin(tmp_path, mock_colony):
    server, port = mock_colony
    transcript = tmp_path / "transcript.jsonl"
    _write_fake_transcript(transcript)

    event = {"transcript_path": str(transcript), "session_id": "abc-123"}
    stdin = (json.dumps(event) + "\n").encode("utf-8")

    result = _run_hook(
        stdin,
        {
            "ANTFARM_URL": f"http://127.0.0.1:{port}",
            "WORKER_ID": "node-1/w-1",
            # Intentionally omit CLAUDE_TRANSCRIPT_PATH — stdin must win.
        },
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert len(server.recorded) == 1
    path, body = server.recorded[0]
    assert path == "/workers/node-1/w-1/usage"
    payload = json.loads(body)
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 50
    assert payload["model"] == "claude-sonnet-4-7"
    assert payload["source"] == "claude_stop_hook"
    assert payload["event_id"]


def test_stop_hook_falls_back_to_env_when_stdin_empty(tmp_path, mock_colony):
    server, port = mock_colony
    transcript = tmp_path / "transcript.jsonl"
    _write_fake_transcript(transcript)

    result = _run_hook(
        b"",
        {
            "ANTFARM_URL": f"http://127.0.0.1:{port}",
            "WORKER_ID": "node-1/w-1",
            "CLAUDE_TRANSCRIPT_PATH": str(transcript),
        },
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert len(server.recorded) == 1


def test_stop_hook_exits_zero_on_malformed_stdin(tmp_path, mock_colony):
    server, port = mock_colony

    result = _run_hook(
        b"not json",
        {
            "ANTFARM_URL": f"http://127.0.0.1:{port}",
            "WORKER_ID": "node-1/w-1",
            # No env fallback — required input is missing after stdin parse fails.
        },
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert len(server.recorded) == 0


def test_stop_hook_exits_zero_when_required_env_missing(tmp_path, mock_colony):
    server, port = mock_colony
    transcript = tmp_path / "transcript.jsonl"
    _write_fake_transcript(transcript)

    event = {"transcript_path": str(transcript)}
    stdin = (json.dumps(event) + "\n").encode("utf-8")

    result = _run_hook(
        stdin,
        {
            "ANTFARM_URL": f"http://127.0.0.1:{port}",
            # WORKER_ID intentionally omitted — hook must skip silently.
        },
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert len(server.recorded) == 0

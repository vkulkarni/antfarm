"""Tests for antfarm.core.activity — text synthesis for the Activity column.

Pure unit tests: no FastAPI, no backend, no IO.
"""

from __future__ import annotations

from antfarm.core import activity


def test_synthesize_text_editing():
    assert activity.synthesize_text("editing", "src/foo.py") == "editing src/foo.py"


def test_synthesize_text_awaiting():
    # "awaiting" has no {target} slot — target is ignored.
    assert activity.synthesize_text("awaiting", "") == "awaiting claude response"
    assert activity.synthesize_text("awaiting", "ignored-target") == "awaiting claude response"


def test_synthesize_text_none():
    assert activity.synthesize_text(None, None) is None
    assert activity.synthesize_text("", "") is None
    assert activity.synthesize_text(None, "") is None


def test_synthesize_text_freeform_unknown_verb():
    # Unknown verb: fall through to "<action> <target>".
    assert activity.synthesize_text("foobar", "baz") == "foobar baz"
    assert activity.synthesize_text("foobar", "") == "foobar"
    assert activity.synthesize_text("", "baz") == "baz"


def test_synthesize_text_running():
    assert activity.synthesize_text("running", "pytest -x") == "running pytest -x"


def test_synthesize_text_running_tests_uses_parens():
    assert activity.synthesize_text("running_tests", "task-001") == "running tests (task-001)"


def test_synthesize_text_fast_forwarding_hyphenates():
    assert activity.synthesize_text("fast_forwarding", "dev") == "fast-forwarding dev"


def test_synthesize_text_planning_ignores_target():
    assert activity.synthesize_text("planning", "") == "planning"
    assert activity.synthesize_text("planning", "ignored") == "planning"


def test_synthesize_text_idle_and_polling():
    assert activity.synthesize_text("idle", "") == "idle"
    assert activity.synthesize_text("polling", "") == "polling"


def test_synthesize_truncates_long_target():
    long = "a" * 200
    out = activity.synthesize_text("editing", long)
    assert out is not None
    # verb "editing " + truncated target = 8 + 60 = 68 chars
    assert len(out) == len("editing ") + 60
    assert out.endswith("...")
    # Truncation keeps the first 57 chars + "..."
    assert out == "editing " + "a" * 57 + "..."


def test_synthesize_does_not_truncate_short_target():
    out = activity.synthesize_text("editing", "a/b.py")
    assert out == "editing a/b.py"


def test_synthesize_strips_whitespace():
    # Whitespace-only inputs count as empty.
    assert activity.synthesize_text("   ", "   ") is None
    assert activity.synthesize_text(" editing ", " file.py ") == "editing file.py"


def test_tool_to_verb_edit_write_read():
    assert activity.tool_to_verb("Edit") == "editing"
    assert activity.tool_to_verb("Write") == "editing"
    assert activity.tool_to_verb("Read") == "reading"


def test_tool_to_verb_bash():
    assert activity.tool_to_verb("Bash") == "running"


def test_tool_to_verb_web():
    assert activity.tool_to_verb("WebFetch") == "searching"
    assert activity.tool_to_verb("WebSearch") == "searching"


def test_tool_to_verb_glob_grep():
    assert activity.tool_to_verb("Glob") == "scanning"
    assert activity.tool_to_verb("Grep") == "scanning"


def test_tool_to_verb_todowrite():
    assert activity.tool_to_verb("TodoWrite") == "planning"


def test_tool_to_verb_unknown_defaults():
    # Unknown tools fall back to their lowercased name.
    assert activity.tool_to_verb("SomeOther") == "someother"
    # Empty/missing → "tool"
    assert activity.tool_to_verb("") == "tool"
    assert activity.tool_to_verb("   ") == "tool"

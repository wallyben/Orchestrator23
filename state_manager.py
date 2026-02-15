#!/usr/bin/env python3
"""Atomic state machine for Orchestrator23.

States:  INIT → GENERATING → TESTING → PATCHING → SUCCESS
                                          │
                                          └──→ FAILED

All mutations persist to state.json before returning.
"""

import json
import os
import uuid
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
STATE_TMP = os.path.join(BASE_DIR, "state.json.tmp")


# ---------------------------------------------------------------------------
# State machine definition
# ---------------------------------------------------------------------------

TRANSITIONS = {
    "INIT":       {"GENERATING"},
    "GENERATING": {"TESTING", "FAILED"},
    "TESTING":    {"SUCCESS", "PATCHING", "FAILED"},
    "PATCHING":   {"TESTING", "FAILED"},
    "SUCCESS":    set(),
    "FAILED":     set(),
}

TERMINAL = {"SUCCESS", "FAILED"}

REQUIRED_KEYS = frozenset({
    "run_id", "spec_file", "state", "retry_count", "max_retries",
    "last_test_exit_code", "last_test_stderr", "last_error",
    "stop_reason", "created_at", "updated_at",
})

STOP_REASONS = {
    "tests_passed":           "All tests passed.",
    "max_retries_exhausted":  "Retry budget exhausted. Tests still failing.",
    "generation_failed":      "Code generation produced no output or raised an error.",
    "generation_empty":       "Code generation returned zero files.",
    "patch_failed":           "Patch operation raised an error.",
    "patch_empty":            "Patch produced no file changes.",
    "safety_violation":       "A generated path attempted to escape the workspace.",
    "test_execution_error":   "The test subprocess could not be launched or timed out.",
    "spec_load_error":        "The spec file could not be read or parsed.",
    "state_corrupted":        "state.json failed validation on load.",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StateError(Exception):
    pass


class IllegalTransition(StateError):
    pass


class CorruptedState(StateError):
    pass


# ---------------------------------------------------------------------------
# Atomic persistence
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(state):
    """Write state dict to STATE_FILE via tmp + os.replace."""
    state["updated_at"] = _now()
    blob = json.dumps(state, indent=2)
    with open(STATE_TMP, "w") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(STATE_TMP, STATE_FILE)


def _validate(state):
    """Raise CorruptedState if the dict is not a valid state document."""
    if not isinstance(state, dict):
        raise CorruptedState("state.json root is not an object")
    missing = REQUIRED_KEYS - set(state.keys())
    if missing:
        raise CorruptedState(f"state.json missing keys: {sorted(missing)}")
    if state["state"] not in TRANSITIONS:
        raise CorruptedState(f"state.json contains unknown state: {state['state']}")
    if not isinstance(state["retry_count"], int) or state["retry_count"] < 0:
        raise CorruptedState(f"retry_count invalid: {state['retry_count']}")
    if not isinstance(state["max_retries"], int) or state["max_retries"] < 1:
        raise CorruptedState(f"max_retries invalid: {state['max_retries']}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def exists():
    """Return True if a state file is present on disk."""
    return os.path.isfile(STATE_FILE)


def load():
    """Load and validate state.json.  Returns None if the file does not exist."""
    if not os.path.isfile(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as fh:
            raw = fh.read()
    except OSError as exc:
        raise CorruptedState(f"Cannot read state.json: {exc}")
    if not raw.strip():
        raise CorruptedState("state.json is empty")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptedState(f"state.json is not valid JSON: {exc}")
    _validate(state)
    return state


def create(spec_file, max_retries):
    """Create a fresh INIT state and persist it.  Returns the state dict."""
    now = _now()
    state = {
        "run_id":             str(uuid.uuid4()),
        "spec_file":          spec_file,
        "state":              "INIT",
        "retry_count":        0,
        "max_retries":        max_retries,
        "last_test_exit_code": None,
        "last_test_stderr":   None,
        "last_error":         None,
        "stop_reason":        None,
        "created_at":         now,
        "updated_at":         now,
    }
    _write_atomic(state)
    return state


def transition(state, target):
    """Move to *target* state.  Raises IllegalTransition on bad edges."""
    current = state["state"]
    allowed = TRANSITIONS.get(current, set())
    if target not in allowed:
        raise IllegalTransition(f"{current} -> {target}")
    state["state"] = target
    _write_atomic(state)


def fail(state, stop_reason, error_message=None):
    """Transition to FAILED with a classified stop reason.

    *stop_reason* must be a key from STOP_REASONS.
    *error_message* is the raw error string stored in last_error.
    """
    if stop_reason not in STOP_REASONS:
        stop_reason_display = stop_reason
    else:
        stop_reason_display = stop_reason
    state["stop_reason"] = stop_reason_display
    state["last_error"] = error_message or STOP_REASONS.get(stop_reason, stop_reason)
    _write_atomic(state)
    transition(state, "FAILED")


def succeed(state):
    """Transition to SUCCESS."""
    state["stop_reason"] = "tests_passed"
    state["last_error"] = None
    _write_atomic(state)
    transition(state, "SUCCESS")


def record_test_result(state, exit_code, stderr):
    """Store the latest test outcome without changing state."""
    state["last_test_exit_code"] = exit_code
    state["last_test_stderr"] = stderr[:10000] if stderr else None
    _write_atomic(state)


def increment_retry(state):
    """Bump retry_count by one and persist."""
    state["retry_count"] += 1
    _write_atomic(state)


def delete():
    """Remove state.json from disk.  No-op if absent."""
    for path in (STATE_FILE, STATE_TMP):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def is_terminal(state):
    """Return True if the run has ended (SUCCESS or FAILED)."""
    return state["state"] in TERMINAL


def is_resumable(state):
    """Return True if the run can be continued."""
    return state["state"] not in TERMINAL


def resume_state(state):
    """Return the effective state to re-enter the loop after a crash.

    PATCHING is not safe to resume mid-way (the patch may or may not have
    landed), so it maps back to TESTING which will re-run and detect whether
    the patch took effect.

    All other non-terminal states resume as-is.
    """
    if state["state"] == "PATCHING":
        transition(state, "TESTING")
        return "TESTING"
    return state["state"]


def summary(state):
    """Return a human-readable one-line summary of the current state."""
    s = state["state"]
    rid = state["run_id"][:8]
    retry = state["retry_count"]
    cap = state["max_retries"]

    if s == "SUCCESS":
        return f"[{rid}] SUCCESS — {STOP_REASONS['tests_passed']}"
    if s == "FAILED":
        reason = state.get("stop_reason", "unknown")
        detail = STOP_REASONS.get(reason, state.get("last_error", reason))
        return f"[{rid}] FAILED — {detail}"
    return f"[{rid}] {s} — retry {retry}/{cap}"


def dump(state):
    """Return the state dict as a formatted JSON string."""
    return json.dumps(state, indent=2)

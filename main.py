#!/usr/bin/env python3
"""Orchestrator23 — Minimal local deterministic build orchestrator.

Usage:
    python main.py run --spec specs/example.json --max-retries 5
    python main.py status
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import claude_client
import test_runner


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
STATE_TMP = os.path.join(BASE_DIR, "state.json.tmp")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
LOGS_DIR = os.path.join(BASE_DIR, "logs")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSITIONS = {
    "INIT":       {"GENERATING"},
    "GENERATING": {"TESTING", "FAILED"},
    "TESTING":    {"DONE", "PATCHING", "FAILED"},
    "PATCHING":   {"TESTING", "FAILED"},
    "DONE":       set(),
    "FAILED":     set(),
}

DEFAULT_MAX_RETRIES = 5
MAX_RETRIES_CEILING = 50
DEFAULT_TEST_TIMEOUT = 300
MAX_TEST_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SafetyViolation(Exception):
    pass


class StateError(Exception):
    pass


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def banner(label, detail=""):
    text = f"[{label}]"
    if detail:
        text += f"  {detail}"
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def info(msg):
    print(f"  {msg}")


def err(msg):
    print(f"  ERROR: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Run log  (append-only, one file per run_id)
# ---------------------------------------------------------------------------

class RunLog:
    def __init__(self, run_id):
        os.makedirs(LOGS_DIR, exist_ok=True)
        self._fh = open(os.path.join(LOGS_DIR, f"{run_id}.log"), "a")

    def write(self, msg):
        ts = datetime.now(timezone.utc).isoformat()
        self._fh.write(f"[{ts}] {msg}\n")
        self._fh.flush()

    def close(self):
        self._fh.close()


# ---------------------------------------------------------------------------
# Safety — workspace containment
# ---------------------------------------------------------------------------

def safe_path(relative):
    """Resolve *relative* into an absolute path that must stay inside workspace/."""
    workspace_real = os.path.realpath(WORKSPACE_DIR)
    target_real = os.path.realpath(os.path.join(WORKSPACE_DIR, relative))
    if target_real != workspace_real and not target_real.startswith(workspace_real + os.sep):
        raise SafetyViolation(
            f"Path escapes workspace: {relative} resolves to {target_real}"
        )
    return target_real


def write_files(files):
    """Write a {relative_path: content} dict into workspace/ with safety checks."""
    written = []
    for rel, content in files.items():
        target = safe_path(rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(content)
        written.append(rel)
    return written


# ---------------------------------------------------------------------------
# State persistence  (atomic via tmp + os.replace)
# ---------------------------------------------------------------------------

def save_state(state):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    blob = json.dumps(state, indent=2)
    with open(STATE_TMP, "w") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(STATE_TMP, STATE_FILE)


def load_state():
    """Return the persisted state dict, or None if no state file exists."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as fh:
            state = json.load(fh)
    except json.JSONDecodeError as exc:
        raise StateError(f"state.json is not valid JSON: {exc}")
    required = {
        "run_id", "spec_file", "state", "retry_count", "max_retries",
        "last_test_exit_code", "last_test_stderr", "last_error",
        "created_at", "updated_at",
    }
    missing = required - set(state.keys())
    if missing:
        raise StateError(f"state.json missing keys: {missing}")
    if state["state"] not in TRANSITIONS:
        raise StateError(f"state.json contains unknown state: {state['state']}")
    return state


def make_state(spec_file, max_retries):
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "run_id": str(uuid.uuid4()),
        "spec_file": spec_file,
        "state": "INIT",
        "retry_count": 0,
        "max_retries": max_retries,
        "last_test_exit_code": None,
        "last_test_stderr": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }
    save_state(state)
    return state


def transition(state, target):
    allowed = TRANSITIONS[state["state"]]
    if target not in allowed:
        raise StateError(f"Illegal transition: {state['state']} -> {target}")
    state["state"] = target
    save_state(state)


# ---------------------------------------------------------------------------
# Spec loader  (JSON or YAML)
# ---------------------------------------------------------------------------

def load_spec(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Spec not found: {path}")
    with open(path, "r") as fh:
        raw = fh.read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Spec must be a JSON/YAML mapping")
    if "test_command" not in data:
        raise ValueError("Spec requires 'test_command'")
    if isinstance(data["test_command"], str):
        data["test_command"] = data["test_command"].split()
    return data


# ---------------------------------------------------------------------------
# Engine — the deterministic retry loop
# ---------------------------------------------------------------------------

def run_engine(spec_file, max_retries):
    clamped = max(1, min(max_retries, MAX_RETRIES_CEILING))
    if clamped != max_retries:
        info(f"max_retries clamped: {max_retries} -> {clamped}")
    max_retries = clamped

    os.makedirs(WORKSPACE_DIR, exist_ok=True)

    # ---- load or create state ----
    try:
        state = load_state()
    except StateError as exc:
        err(str(exc))
        return 3

    if state is not None:
        # Resume path
        if state["state"] in {"DONE", "FAILED"}:
            banner(state["state"])
            if state["state"] == "DONE":
                info("Run already completed.  All tests passed.")
                return 0
            info(f"Run already failed: {state['last_error']}")
            return 1

        banner("RESUME", f"run={state['run_id']}")
        info(f"State: {state['state']}  |  Retry: {state['retry_count']}/{state['max_retries']}")
    else:
        # Fresh run — spec_file is required (caller guarantees this)
        state = make_state(spec_file, max_retries)
        banner("INIT", f"run={state['run_id']}")
        info(f"Spec:        {state['spec_file']}")
        info(f"Max retries: {state['max_retries']}")

    rlog = RunLog(state["run_id"])
    rlog.write(f"Engine start | state={state['state']} | retry={state['retry_count']}")

    # ---- load spec ----
    try:
        spec = load_spec(state["spec_file"])
    except Exception as exc:
        err(f"Spec load failed: {exc}")
        rlog.write(f"Spec load error: {exc}")
        state["last_error"] = str(exc)
        save_state(state)
        if state["state"] == "INIT":
            transition(state, "GENERATING")
        transition(state, "FAILED")
        rlog.close()
        return 1

    test_cmd = spec["test_command"]
    test_timeout = min(int(spec.get("test_timeout", DEFAULT_TEST_TIMEOUT)), MAX_TEST_TIMEOUT)

    # ==================================================================
    # INIT  →  GENERATING  →  TESTING
    # ==================================================================
    if state["state"] == "INIT":
        transition(state, "GENERATING")
        banner("GENERATING")
        rlog.write("Generating code from spec")

        try:
            files = claude_client.generate(spec, WORKSPACE_DIR)
        except Exception as exc:
            err(f"Generation failed: {exc}")
            rlog.write(f"Generation error: {exc}")
            state["last_error"] = f"Generation failed: {exc}"
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        if not files:
            err("Generation produced no output")
            rlog.write("Generation produced no output")
            state["last_error"] = "Generation produced no output"
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        try:
            written = write_files(files)
            for path in written:
                info(f"  wrote: {path}")
            rlog.write(f"Generated {len(written)} file(s): {written}")
        except SafetyViolation as exc:
            err(str(exc))
            rlog.write(f"Safety violation: {exc}")
            state["last_error"] = str(exc)
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 2

        transition(state, "TESTING")

    # ==================================================================
    # PATCHING crash recovery  →  re-enter TESTING
    # ==================================================================
    if state["state"] == "PATCHING":
        banner("RESUME", "Crashed during patch — re-running tests")
        rlog.write("Resumed from PATCHING, re-entering TESTING")
        transition(state, "TESTING")

    # ==================================================================
    # TESTING  ⇄  PATCHING   (bounded retry loop)
    # ==================================================================
    while state["state"] == "TESTING":
        attempt = state["retry_count"] + 1
        total_attempts = state["max_retries"] + 1
        banner("TESTING", f"attempt {attempt}/{total_attempts}")
        rlog.write(f"Running tests | attempt {attempt}/{total_attempts}")

        # ---- run tests ----
        try:
            exit_code, stdout, stderr = test_runner.run(
                workspace_dir=WORKSPACE_DIR,
                command=test_cmd,
                timeout=test_timeout,
            )
        except Exception as exc:
            err(f"Test execution error: {exc}")
            rlog.write(f"Test execution error: {exc}")
            state["last_error"] = f"Test execution error: {exc}"
            state["last_test_exit_code"] = None
            state["last_test_stderr"] = None
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        state["last_test_exit_code"] = exit_code
        state["last_test_stderr"] = stderr[:10000] if stderr else None
        save_state(state)

        # ---- tests passed ----
        if exit_code == 0:
            banner("DONE", "All tests passed")
            rlog.write("Tests passed")
            transition(state, "DONE")
            rlog.close()
            return 0

        # ---- tests failed — report ----
        info(f"Tests failed  (exit {exit_code})")
        if stderr:
            for line in stderr.strip().splitlines()[-20:]:
                info(f"  | {line}")
        rlog.write(f"Tests failed | exit={exit_code}")

        # ---- budget exhausted ----
        if state["retry_count"] >= state["max_retries"]:
            banner("FAILED", f"Exhausted {state['max_retries']} retries")
            rlog.write("Max retries exhausted")
            state["last_error"] = f"Tests failed after {state['max_retries']} retries"
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        # ---- patch ----
        transition(state, "PATCHING")
        banner("PATCHING", f"retry {state['retry_count'] + 1}/{state['max_retries']}")
        rlog.write("Calling claude_client.patch")

        try:
            patch_files = claude_client.patch(spec, stderr, WORKSPACE_DIR)
        except Exception as exc:
            err(f"Patch failed: {exc}")
            rlog.write(f"Patch error: {exc}")
            state["last_error"] = f"Patch failed: {exc}"
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        if not patch_files:
            err("Patch produced no changes")
            rlog.write("Patch produced no changes")
            state["last_error"] = "Patch produced no changes"
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 1

        try:
            written = write_files(patch_files)
            for path in written:
                info(f"  patched: {path}")
            rlog.write(f"Patched {len(written)} file(s): {written}")
        except SafetyViolation as exc:
            err(str(exc))
            rlog.write(f"Safety violation during patch: {exc}")
            state["last_error"] = str(exc)
            save_state(state)
            transition(state, "FAILED")
            rlog.close()
            return 2

        state["retry_count"] += 1
        transition(state, "TESTING")

    rlog.close()
    return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_run(args):
    # Check whether we are resuming or starting fresh
    existing = None
    try:
        existing = load_state()
    except StateError:
        pass  # engine will catch and report

    if existing is None and args.spec is None:
        err("--spec is required for a new run")
        return 1

    spec_path = os.path.abspath(args.spec) if args.spec else None

    try:
        return run_engine(
            spec_file=spec_path if spec_path else "",
            max_retries=args.max_retries,
        )
    except KeyboardInterrupt:
        print("\n  Interrupted. State preserved.")
        print("  Resume: python main.py run")
        return 130


def cmd_status(_args):
    try:
        state = load_state()
    except StateError as exc:
        err(str(exc))
        return 3

    if state is None:
        info("No active run.  state.json does not exist.")
        return 0

    print(json.dumps(state, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="orchestrator23",
        description="Minimal local deterministic build orchestrator.",
    )
    subs = parser.add_subparsers(dest="command")

    p_run = subs.add_parser("run", help="Execute the build loop (or resume)")
    p_run.add_argument("--spec", help="Path to spec file (required for new run)")
    p_run.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Max patch-retry cycles (default {DEFAULT_MAX_RETRIES}, ceiling {MAX_RETRIES_CEILING})",
    )

    subs.add_parser("status", help="Print current state.json")

    args = parser.parse_args()

    if args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "status":
        sys.exit(cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

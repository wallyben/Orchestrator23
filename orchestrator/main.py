#!/usr/bin/env python3
"""
Orchestrator23 — Deterministic local code generation loop.

Reads a product spec, generates project files via Claude,
runs tests, and iterates until tests pass or max retries exhausted.
"""

import os
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timezone

from claude_client import ClaudeClient
from config import Config, MAX_RETRIES_HARD_CAP, load_config
from logger import Logger
from state_manager import (
    PHASE_FAILED,
    PHASE_GENERATED,
    PHASE_GENERATING,
    PHASE_INIT,
    PHASE_PATCHING,
    PHASE_SUCCESS,
    PHASE_TESTED,
    PHASE_TESTING,
    TERMINAL_PHASES,
    StateManager,
    compute_spec_hash,
)
from test_runner import TestRunner
from tool_registry import ToolRegistry

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def generate_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_spec(spec_path: str) -> str:
    with open(spec_path, "r", encoding="utf-8") as f:
        return f.read()


def read_workspace_files(workspace_path: str) -> dict[str, str]:
    files = {}
    for root, _dirs, filenames in os.walk(workspace_path):
        for fname in filenames:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, workspace_path)
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    files[rel_path] = f.read()
            except (UnicodeDecodeError, OSError):
                continue
    return files


def _safe_rel_path(workspace_path: str, rel_path: str) -> str | None:
    rel_path = rel_path.lstrip("/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    normalized = os.path.normpath(rel_path)
    if normalized.startswith("..") or normalized.startswith(os.sep):
        return None
    if "\x00" in normalized:
        return None
    full = os.path.join(workspace_path, normalized)
    if not os.path.abspath(full).startswith(os.path.abspath(workspace_path) + os.sep):
        return None
    return full


def write_workspace_files(workspace_path: str, files: dict[str, str], logger: Logger):
    written = []
    for rel_path, content in files.items():
        full_path = _safe_rel_path(workspace_path, rel_path)
        if full_path is None:
            logger.warn("workspace_write_path_rejected", {"path": rel_path})
            continue

        parent = os.path.dirname(full_path)
        os.makedirs(parent, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp", prefix=".ws_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, full_path)
            written.append(rel_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    logger.info("workspace_files_written", {"count": len(written), "files": written})


def clear_workspace(workspace_path: str):
    ws = os.path.abspath(workspace_path)
    if ws in ("/", os.path.expanduser("~")):
        raise RuntimeError(f"Refusing to clear dangerous path: {ws}")
    if os.path.isdir(ws):
        shutil.rmtree(ws)
    os.makedirs(ws, exist_ok=True)


def run(argv=None) -> int:
    config = load_config(argv)

    spec = read_spec(config.spec_path)
    spec_hash = compute_spec_hash(spec)

    prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
    prev_sigint = signal.signal(signal.SIGINT, _handle_signal)

    state = None
    if config.resume:
        boot_logger = Logger(config.logs_path, "boot")
        try:
            state_mgr_probe = StateManager(config.state_path, boot_logger)
            state = state_mgr_probe.load_existing(spec_hash)
        finally:
            boot_logger.close()

    if state is not None:
        run_id = state["run_id"]
    else:
        run_id = generate_run_id()

    logger = Logger(config.logs_path, run_id)
    state_mgr = StateManager(config.state_path, logger)

    if state is not None:
        state_mgr._state = state.copy()
        logger.info(
            "resumed_run",
            {"run_id": run_id, "phase": state["phase"], "attempt": state["attempt"]},
        )
    else:
        clear_workspace(config.workspace_path)
        state = state_mgr.create_fresh(run_id, config.max_retries, spec_hash)
        logger.info("fresh_run", {"run_id": run_id, "max_retries": config.max_retries})

    client = ClaudeClient(config, logger)
    runner = TestRunner(config, logger)

    registry = ToolRegistry(logger)
    registry.register(runner)

    try:
        exit_code = _loop(state_mgr, client, registry, spec, config, logger)
    except KeyboardInterrupt:
        logger.warn("interrupted_by_user")
        print("\nInterrupted. State saved. Resume with --resume.", file=sys.stderr)
        exit_code = 130
    except Exception as e:
        logger.error(
            "unhandled_exception",
            {"error": str(e), "type": type(e).__name__},
        )
        print(f"\nFATAL: {type(e).__name__}: {e}", file=sys.stderr)
        print("State saved. Resume with --resume.", file=sys.stderr)
        exit_code = 2
    finally:
        logger.close()
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)

    return exit_code


def _check_shutdown(logger: Logger):
    if _shutdown_requested:
        logger.warn("shutdown_signal_received")
        raise KeyboardInterrupt("Shutdown signal received")


def _loop(
    state_mgr: StateManager,
    client: ClaudeClient,
    registry: ToolRegistry,
    spec: str,
    config: Config,
    logger: Logger,
) -> int:

    absolute_max_iterations = config.max_retries + 10
    iteration = 0

    while True:
        iteration += 1
        if iteration > absolute_max_iterations:
            logger.error(
                "absolute_iteration_limit",
                {"iteration": iteration, "limit": absolute_max_iterations},
            )
            try:
                state_mgr.update(phase=PHASE_FAILED)
            except ValueError:
                pass
            print(
                f"\nFAILED — absolute iteration safety limit ({absolute_max_iterations}) hit.",
                file=sys.stderr,
            )
            return 1

        state = state_mgr.get()
        attempt = state["attempt"]
        phase = state["phase"]

        _check_shutdown(logger)

        if phase in TERMINAL_PHASES:
            return 0 if phase == PHASE_SUCCESS else 1

        if attempt > config.max_retries:
            logger.error(
                "max_retries_exceeded",
                {"attempt": attempt, "max_retries": config.max_retries},
            )
            state_mgr.update(phase=PHASE_FAILED)
            print(
                f"\nFAILED — max retries ({config.max_retries}) exceeded.",
                file=sys.stderr,
            )
            return 1

        # --- RESUME RECOVERY: interrupted mid-generation ---
        if phase in (PHASE_GENERATING, PHASE_PATCHING):
            logger.warn("resume_from_interrupted_generation", {"phase": phase})
            if attempt == 0:
                state_mgr.update(phase=PHASE_INIT)
            else:
                state_mgr.update(phase=PHASE_TESTED)
            continue

        # --- RESUME RECOVERY: interrupted mid-testing ---
        if phase == PHASE_TESTING:
            logger.warn("resume_from_interrupted_testing")
            state_mgr.update(phase=PHASE_GENERATED)
            continue

        # --- GENERATE or PATCH ---
        if phase in (PHASE_INIT, PHASE_TESTED):
            _check_shutdown(logger)

            if attempt == 0:
                print(
                    f"\n[attempt {attempt}] Generating initial project...",
                    file=sys.stderr,
                )
                logger.info("generating_initial", {"attempt": attempt})
                state_mgr.update(phase=PHASE_GENERATING)
                try:
                    files = client.generate_project(spec)
                except RuntimeError as e:
                    logger.error("generation_failed", {"error": str(e)})
                    state_mgr.update(phase=PHASE_FAILED)
                    print(f"\nFAILED — generation error: {e}", file=sys.stderr)
                    return 1
            else:
                print(
                    f"\n[attempt {attempt}/{config.max_retries}] Patching based on test failures...",
                    file=sys.stderr,
                )
                logger.info("patching", {"attempt": attempt})
                state_mgr.update(phase=PHASE_PATCHING)
                workspace_files = read_workspace_files(config.workspace_path)
                try:
                    files = client.generate_patch(
                        spec, workspace_files, state["last_test_output"]
                    )
                except RuntimeError as e:
                    logger.error("patch_failed", {"error": str(e)})
                    state_mgr.update(phase=PHASE_FAILED)
                    print(f"\nFAILED — patch error: {e}", file=sys.stderr)
                    return 1

            write_workspace_files(config.workspace_path, files, logger)
            state_mgr.update(
                phase=PHASE_GENERATED,
                attempt_files=list(files.keys()),
            )
            continue

        # --- TEST (run all registered tools) ---
        if phase == PHASE_GENERATED:
            _check_shutdown(logger)

            tool_names = registry.tool_names
            print(
                f"  Running tools: {', '.join(tool_names)}...",
                file=sys.stderr,
            )
            logger.info("testing", {"attempt": attempt, "tools": tool_names})
            state_mgr.update(phase=PHASE_TESTING)

            results = registry.run_all()
            all_passed = all(r.passed for r in results)
            combined_output = "\n".join(
                f"--- [{r.tool_name}] (rc={r.return_code}) ---\n{r.output}"
                for r in results
            )

            state_mgr.update(
                phase=PHASE_TESTED,
                test_passed=all_passed,
                last_test_output=combined_output,
            )
            continue

        # --- EVALUATE ---
        if phase == PHASE_TESTED:
            if state["test_passed"]:
                state_mgr.update(phase=PHASE_SUCCESS)
                print(
                    f"\nSUCCESS — all tests passed on attempt {attempt}.",
                    file=sys.stderr,
                )
                return 0
            else:
                logger.warn(
                    "tests_failed",
                    {
                        "attempt": attempt,
                        "output_tail": state["last_test_output"][-300:],
                    },
                )
                print(
                    f"  Tests FAILED on attempt {attempt}.",
                    file=sys.stderr,
                )
                state_mgr.update(attempt=attempt + 1)
                continue

        logger.error("unknown_phase", {"phase": phase})
        print(f"\nFATAL — unknown phase: {phase}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())

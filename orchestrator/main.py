#!/usr/bin/env python3
"""
Orchestrator23 — Deterministic local code generation loop.

Reads a product spec, generates project files via Claude,
runs tests, and iterates until tests pass or max retries exhausted.
"""

import os
import shutil
import sys
from datetime import datetime, timezone

from claude_client import ClaudeClient
from config import load_config
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
    StateManager,
    compute_spec_hash,
)
from test_runner import TestRunner


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


def write_workspace_files(workspace_path: str, files: dict[str, str]):
    for rel_path, content in files.items():
        rel_path = rel_path.lstrip("/").lstrip("./")
        full_path = os.path.join(workspace_path, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)


def clear_workspace(workspace_path: str):
    if os.path.isdir(workspace_path):
        shutil.rmtree(workspace_path)
    os.makedirs(workspace_path, exist_ok=True)


def run(argv=None) -> int:
    config = load_config(argv)

    spec = read_spec(config.spec_path)
    spec_hash = compute_spec_hash(spec)

    state_mgr = StateManager(config.state_path, Logger(config.logs_path, "boot"))

    state = None
    if config.resume:
        boot_logger = Logger(config.logs_path, "boot")
        state_mgr_probe = StateManager(config.state_path, boot_logger)
        state = state_mgr_probe.load_existing(spec_hash)
        boot_logger.close()

    if state is not None:
        run_id = state["run_id"]
    else:
        run_id = generate_run_id()

    logger = Logger(config.logs_path, run_id)
    state_mgr = StateManager(config.state_path, logger)

    if state is not None:
        state_mgr._state = state
        logger.info("resumed_run", {"run_id": run_id, "phase": state["phase"], "attempt": state["attempt"]})
    else:
        clear_workspace(config.workspace_path)
        state = state_mgr.create_fresh(run_id, config.max_retries, spec_hash)
        logger.info("fresh_run", {"run_id": run_id, "max_retries": config.max_retries})

    client = ClaudeClient(config, logger)
    runner = TestRunner(config, logger)

    try:
        exit_code = _loop(state_mgr, client, runner, spec, config, logger)
    except KeyboardInterrupt:
        logger.warn("interrupted_by_user")
        exit_code = 130
    except Exception as e:
        logger.error("unhandled_exception", {"error": str(e), "type": type(e).__name__})
        exit_code = 2
    finally:
        logger.close()

    return exit_code


def _loop(
    state_mgr: StateManager,
    client: ClaudeClient,
    runner: TestRunner,
    spec: str,
    config,
    logger: Logger,
) -> int:
    state = state_mgr.get()

    while True:
        attempt = state["attempt"]
        phase = state["phase"]

        if attempt > config.max_retries:
            logger.error(
                "max_retries_exceeded",
                {"attempt": attempt, "max_retries": config.max_retries},
            )
            state_mgr.update(phase=PHASE_FAILED)
            print(f"\nFAILED — max retries ({config.max_retries}) exceeded.", file=sys.stderr)
            return 1

        # --- GENERATE or PATCH ---
        if phase in (PHASE_INIT, PHASE_TESTED):
            if attempt == 0:
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

            write_workspace_files(config.workspace_path, files)
            state_mgr.update(
                phase=PHASE_GENERATED,
                attempt_files=list(files.keys()),
            )
            state = state_mgr.get()

        # --- TEST ---
        if state["phase"] == PHASE_GENERATED:
            logger.info("testing", {"attempt": attempt})
            state_mgr.update(phase=PHASE_TESTING)
            result = runner.run_tests()
            state_mgr.update(
                phase=PHASE_TESTED,
                test_passed=result.passed,
                last_test_output=result.output,
            )
            state = state_mgr.get()

        # --- EVALUATE ---
        if state["phase"] == PHASE_TESTED:
            if state["test_passed"]:
                state_mgr.update(phase=PHASE_SUCCESS)
                print(
                    f"\nSUCCESS — tests passed on attempt {attempt}.",
                    file=sys.stderr,
                )
                return 0
            else:
                logger.warn(
                    "tests_failed",
                    {"attempt": attempt, "output_tail": state["last_test_output"][-500:]},
                )
                print(
                    f"  Attempt {attempt} failed. Retrying...",
                    file=sys.stderr,
                )
                state_mgr.update(attempt=attempt + 1)
                state = state_mgr.get()
                continue

        # Safety: if we land in an unexpected phase after resume, handle it
        if state["phase"] in (PHASE_GENERATING, PHASE_PATCHING):
            logger.warn("resume_from_mid_generation", {"phase": state["phase"]})
            state_mgr.update(phase=PHASE_INIT if state["attempt"] == 0 else PHASE_TESTED)
            state = state_mgr.get()
            continue

        if state["phase"] == PHASE_TESTING:
            logger.warn("resume_from_mid_testing", {"phase": state["phase"]})
            state_mgr.update(phase=PHASE_GENERATED)
            state = state_mgr.get()
            continue

        if state["phase"] in (PHASE_SUCCESS, PHASE_FAILED):
            return 0 if state["phase"] == PHASE_SUCCESS else 1


if __name__ == "__main__":
    sys.exit(run())

#!/usr/bin/env python3
"""Test runner for Orchestrator23.

Executes a test command inside workspace/ via subprocess.
Returns (exit_code, stdout, stderr).
"""

import os
import subprocess


ENV_ALLOWLIST = ("PATH", "HOME", "LANG")


def run(*, workspace_dir, command, timeout):
    """Run *command* inside *workspace_dir* with containment.

    Args:
        workspace_dir: Absolute path to the workspace directory.
        command:        List of strings (shell=False).
        timeout:        Max seconds before the subprocess is killed.

    Returns:
        (exit_code, stdout, stderr) — all strings decoded as utf-8.

    Raises:
        subprocess.TimeoutExpired: if the process exceeds *timeout*.
        FileNotFoundError:         if the command binary is not found.
        OSError:                   on other launch failures.
    """
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["PYTHONPATH"] = workspace_dir

    result = subprocess.run(
        command,
        cwd=workspace_dir,
        capture_output=True,
        timeout=timeout,
        shell=False,
        env=env,
    )

    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    return result.returncode, stdout, stderr

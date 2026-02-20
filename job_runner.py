"""job_runner.py – spec-driven job orchestrator.

Job file schema::

    {
        "steps": [
            {
                "id": "step-001",
                "prompt": "Add a subtract(a, b) function to app/math_utils.py",
                "verify": {
                    "cmd": "python -m pytest -q",
                    "cwd": "workspace"
                }
            }
        ]
    }

Per-step logs are written under:
    logs/job/<job_id>/<step_id>/llm_patch.json
    logs/job/<job_id>/<step_id>/patch_manifest.json
    logs/job/<job_id>/<step_id>/verify_output.txt

Progress is persisted in state.json under key "job":
    {"file": "<path>", "current_step": <int>, "stop_reason": "<str>"}
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from config import BASE_DIR, LOGS_DIR, STATE_FILE, WORKSPACE_DIR
from llm.openai_provider import OpenAIProvider
from patch_enforcer import PatchEnforcer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [job_runner] {msg}", flush=True)


def _load_state() -> dict:
    state_path = Path(STATE_FILE)
    if not state_path.exists():
        return {}
    content = state_path.read_text(encoding="utf-8").strip()
    return json.loads(content) if content else {}


def _save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_cwd(raw_cwd) -> str:
    """Return an absolute path string for a verify.cwd value.

    If *raw_cwd* is already absolute, return it unchanged.
    If relative, resolve it against BASE_DIR (repo root).
    Falls back to BASE_DIR when *raw_cwd* is None/empty.
    """
    if not raw_cwd:
        return str(BASE_DIR)
    p = Path(raw_cwd)
    if not p.is_absolute():
        p = Path(BASE_DIR) / p
    return str(p)


def _run_verify(cmd: str, cwd: str) -> tuple:
    """Run *cmd* with shell=True in *cwd*.

    Returns (returncode, output_text) where output_text combines stdout + stderr.
    """
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    output = (
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}\n"
        f"--- returncode: {result.returncode} ---\n"
    )
    return result.returncode, output


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_job(job_file: str) -> bool:
    """Execute all steps in *job_file*.

    Returns True when all steps succeed, False when a step fails
    permanently (after the repair loop).  Calls sys.exit(1) for
    configuration errors (missing file, invalid JSON, bad schema).
    """
    job_path = Path(job_file)

    # ------------------------------------------------------------------
    # Validate job file
    # ------------------------------------------------------------------
    if not job_path.exists():
        _log(f"ERROR: job file not found: {job_file}")
        sys.exit(1)

    try:
        raw_text = job_path.read_text(encoding="utf-8")
        job = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        _log(f"ERROR: job file contains invalid JSON: {exc}")
        sys.exit(1)

    if not isinstance(job, dict) or "steps" not in job:
        _log("ERROR: job file must be a JSON object with a top-level 'steps' list")
        sys.exit(1)

    steps = job["steps"]
    if not isinstance(steps, list) or len(steps) == 0:
        _log("ERROR: 'steps' must be a non-empty list")
        sys.exit(1)

    # job_id: use filename stem (without extension) for log directory names
    job_id = job_path.stem

    # ------------------------------------------------------------------
    # Resume support via state.json
    # ------------------------------------------------------------------
    state = _load_state()
    job_state = state.get("job", {})

    # Reset progress whenever a different job file is being run
    if job_state.get("file") != str(job_path):
        job_state = {"file": str(job_path), "current_step": 0}

    current_step: int = int(job_state.get("current_step", 0))

    _log(
        f"Job '{job_id}': {len(steps)} step(s) total, "
        f"resuming from step index {current_step}"
    )

    provider = OpenAIProvider()
    enforcer = PatchEnforcer(WORKSPACE_DIR)

    # ------------------------------------------------------------------
    # Step loop
    # ------------------------------------------------------------------
    for idx in range(current_step, len(steps)):
        step = steps[idx]

        # Validate required step keys
        for required_key in ("id", "prompt"):
            if required_key not in step:
                _log(
                    f"ERROR: step at index {idx} is missing required key "
                    f"'{required_key}'"
                )
                job_state.update(
                    {"current_step": idx, "stop_reason": "INVALID_STEP_SCHEMA"}
                )
                state["job"] = job_state
                _save_state(state)
                return False

        step_id = str(step["id"])
        prompt_text: str = step["prompt"]
        verify: dict = step.get("verify", {})
        verify_cmd: str = verify.get("cmd", "")
        verify_cwd: str = _resolve_cwd(verify.get("cwd"))

        # Per-step log directory: logs/job/<job_id>/<step_id>/
        step_log_dir = _ensure_dir(
            Path(LOGS_DIR) / "job" / job_id / step_id
        )

        _log(f"Step [{step_id}] ({idx + 1}/{len(steps)}): calling LLM for patch…")

        # --------------------------------------------------------------
        # 1. Generate patch via LLM
        # --------------------------------------------------------------
        try:
            patch = provider.patch_from_prompt(prompt_text)
        except Exception as exc:  # noqa: BLE001
            _log(f"Step [{step_id}]: LLM call failed: {exc}")
            job_state.update(
                {"current_step": idx, "stop_reason": "LLM_ERROR"}
            )
            state["job"] = job_state
            _save_state(state)
            return False

        # Save raw patch dict
        (step_log_dir / "llm_patch.json").write_text(
            json.dumps(patch, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # --------------------------------------------------------------
        # 2. Apply patch to workspace
        # --------------------------------------------------------------
        enforcer.apply(patch)
        files_written = [entry["path"] for entry in patch.get("files", [])]
        (step_log_dir / "patch_manifest.json").write_text(
            json.dumps({"files_written": files_written}, indent=2),
            encoding="utf-8",
        )
        _log(f"Step [{step_id}]: applied patch ({len(files_written)} file(s))")

        # --------------------------------------------------------------
        # 3. Verify
        # --------------------------------------------------------------
        if verify_cmd:
            _log(f"Step [{step_id}]: verify → {verify_cmd!r} in {verify_cwd!r}")

            rc, verify_out = _run_verify(verify_cmd, verify_cwd)
            (step_log_dir / "verify_output.txt").write_text(
                verify_out, encoding="utf-8"
            )

            if rc != 0:
                _log(
                    f"Step [{step_id}]: verify failed (rc={rc}), "
                    "invoking repair loop (`python main.py run`)…"
                )

                # Repair: invoke existing retry/test runner
                subprocess.run(
                    [sys.executable, "main.py", "run"],
                    cwd=BASE_DIR,
                )

                # Re-verify after repair
                _log(f"Step [{step_id}]: re-verifying after repair…")
                rc2, verify_out2 = _run_verify(verify_cmd, verify_cwd)

                # Append re-verify output to the same log file
                with (step_log_dir / "verify_output.txt").open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(
                        f"\n--- RE-VERIFY (after repair) ---\n{verify_out2}"
                    )

                if rc2 != 0:
                    _log(
                        f"Step [{step_id}]: still failing after repair (rc={rc2}). "
                        "Stopping job."
                    )
                    job_state.update(
                        {
                            "current_step": idx,
                            "stop_reason": "VERIFY_FAILED_AFTER_REPAIR",
                        }
                    )
                    state["job"] = job_state
                    _save_state(state)
                    return False

        # --------------------------------------------------------------
        # 4. Persist progress
        # --------------------------------------------------------------
        job_state["current_step"] = idx + 1
        job_state.pop("stop_reason", None)
        state["job"] = job_state
        _save_state(state)

        _log(f"Step [{step_id}]: complete.")

    _log(f"Job '{job_id}': all {len(steps)} step(s) completed successfully.")
    return True

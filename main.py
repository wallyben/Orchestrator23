import argparse
import json
import os
import sys
import time
import subprocess
import traceback

import config


VALID_STATES = ["INIT", "GENERATING", "TESTING", "PATCHING", "SUCCESS", "FAILED"]


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(state):
    atomic_write_json(config.STATE_FILE, state)


def ensure_dirs():
    os.makedirs(config.WORKSPACE_DIR, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(os.path.join(config.LOGS_DIR, "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_tests():
    try:
        result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q"],
            cwd=config.WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=config.SUBPROCESS_TIMEOUT_SECONDS,
            shell=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "TEST_TIMEOUT"
    except Exception as e:
        return False, f"TEST_RUN_ERROR: {e}"


def run_loop():
    ensure_dirs()

    state = load_state()
    if not state:
        state = {
            "status": "INIT",
            "retry_count": 0,
            "max_retries": config.MAX_RETRIES,
            "last_test_output": "",
            "stop_reason": "",
            "updated_at": time.time(),
        }
        save_state(state)

    while True:
        status = state.get("status")

        if status not in VALID_STATES:
            state["status"] = "FAILED"
            state["stop_reason"] = "INVALID_STATE"
            save_state(state)
            log("FAILED: invalid state")
            return 3

        if status in ["SUCCESS", "FAILED"]:
            log(f"TERMINAL: {status}")
            return 0 if status == "SUCCESS" else 2

        if status == "INIT":
            log("INIT → GENERATING")
            state["status"] = "GENERATING"
            save_state(state)
            continue

        if status == "GENERATING":
            log("GENERATING → TESTING (v0 no generation)")
            state["status"] = "TESTING"
            save_state(state)
            continue

        if status == "TESTING":
            log("TESTING...")
            passed, output = run_tests()
            state["last_test_output"] = output
            state["updated_at"] = time.time()

            if passed:
                state["status"] = "SUCCESS"
                state["stop_reason"] = "TESTS_PASSED"
                save_state(state)
                log("SUCCESS: tests passed")
                continue

            if state["retry_count"] >= state["max_retries"]:
                state["status"] = "FAILED"
                state["stop_reason"] = "MAX_RETRIES_EXHAUSTED"
                save_state(state)
                log("FAILED: max retries reached")
                continue

            state["status"] = "PATCHING"
            save_state(state)
            continue

        if status == "PATCHING":
            state["retry_count"] += 1
            state["status"] = "TESTING"
            save_state(state)
            log(f"PATCHING (noop) → TESTING (retry {state['retry_count']})")
            continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "status"])
    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(load_state(), indent=2))
        return 0

    if args.command == "run":
        return run_loop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. State preserved.")
        sys.exit(130)
    except Exception:
        print("Fatal error:")
        print(traceback.format_exc())
        sys.exit(1)

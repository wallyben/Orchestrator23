import os
import json
import subprocess
import time
import sys
from pathlib import Path

STATE_FILE = "state.json"
WORKSPACE_DIR = Path("workspace")
SPEC_FILE = Path("spec.md")
MAX_RETRIES = 5


def load_state():
    if not Path(STATE_FILE).exists():
        return {}
    with open(STATE_FILE, "r") as f:
        content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def generate_from_spec():
    log("GENERATING from spec.md")

    if not SPEC_FILE.exists():
        return

    spec = SPEC_FILE.read_text()

    if "multiply" in spec:
        (WORKSPACE_DIR / "app").mkdir(exist_ok=True)
        (WORKSPACE_DIR / "tests").mkdir(exist_ok=True)

        (WORKSPACE_DIR / "app" / "__init__.py").write_text("# init\n")

        (WORKSPACE_DIR / "app" / "math_utils.py").write_text(
            "def multiply(a, b):\n"
            "    return a * b\n"
        )

        (WORKSPACE_DIR / "tests" / "test_math.py").write_text(
            "from app.math_utils import multiply\n\n"
            "def test_multiply():\n"
            "    assert multiply(3, 4) == 12\n"
        )


def run_tests():
    log("TESTING...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(WORKSPACE_DIR),
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stdout + result.stderr


def run():
    state = load_state()
    retry_count = state.get("retry_count", 0)

    log("INIT → GENERATING")
    generate_from_spec()

    while retry_count < MAX_RETRIES:
        success, output = run_tests()

        if success:
            log("SUCCESS: tests passed")
            state.update({
                "status": "SUCCESS",
                "retry_count": retry_count,
                "updated_at": time.time(),
            })
            save_state(state)
            log("TERMINAL: SUCCESS")
            return

        retry_count += 1
        log(f"PATCHING (noop) → TESTING (retry {retry_count})")

    state.update({
        "status": "FAILED",
        "retry_count": retry_count,
        "stop_reason": "MAX_RETRIES_EXHAUSTED",
        "updated_at": time.time(),
    })
    save_state(state)
    log("TERMINAL: FAILED")


def status():
    print(json.dumps(load_state(), indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py [run|status]")
    elif sys.argv[1] == "run":
        run()
    elif sys.argv[1] == "status":
        status()

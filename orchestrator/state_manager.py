import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from logger import Logger

PHASE_INIT = "init"
PHASE_GENERATING = "generating"
PHASE_GENERATED = "generated"
PHASE_TESTING = "testing"
PHASE_TESTED = "tested"
PHASE_PATCHING = "patching"
PHASE_SUCCESS = "success"
PHASE_FAILED = "failed"

ALL_PHASES = frozenset({
    PHASE_INIT,
    PHASE_GENERATING,
    PHASE_GENERATED,
    PHASE_TESTING,
    PHASE_TESTED,
    PHASE_PATCHING,
    PHASE_SUCCESS,
    PHASE_FAILED,
})

TERMINAL_PHASES = frozenset({PHASE_SUCCESS, PHASE_FAILED})

VALID_TRANSITIONS = {
    PHASE_INIT: frozenset({PHASE_GENERATING}),
    PHASE_GENERATING: frozenset({PHASE_GENERATED, PHASE_FAILED, PHASE_INIT}),
    PHASE_GENERATED: frozenset({PHASE_TESTING}),
    PHASE_TESTING: frozenset({PHASE_TESTED, PHASE_GENERATED}),
    PHASE_TESTED: frozenset({PHASE_PATCHING, PHASE_SUCCESS, PHASE_FAILED}),
    PHASE_PATCHING: frozenset({PHASE_GENERATED, PHASE_FAILED, PHASE_TESTED}),
    PHASE_SUCCESS: frozenset(),
    PHASE_FAILED: frozenset(),
}

REQUIRED_STATE_KEYS = frozenset({
    "run_id", "phase", "attempt", "max_retries",
    "test_passed", "last_test_output", "attempt_files",
    "spec_hash", "created_at", "updated_at",
})


def compute_spec_hash(spec_content: str) -> str:
    return "sha256:" + hashlib.sha256(spec_content.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_state_schema(data: dict) -> list[str]:
    errors = []
    missing = REQUIRED_STATE_KEYS - set(data.keys())
    if missing:
        errors.append(f"Missing keys: {sorted(missing)}")
    if "phase" in data and data["phase"] not in ALL_PHASES:
        errors.append(f"Unknown phase: {data['phase']}")
    if "attempt" in data and (not isinstance(data["attempt"], int) or data["attempt"] < 0):
        errors.append(f"Invalid attempt: {data['attempt']}")
    if "max_retries" in data and (not isinstance(data["max_retries"], int) or data["max_retries"] < 0):
        errors.append(f"Invalid max_retries: {data['max_retries']}")
    return errors


class StateManager:
    def __init__(self, state_path: str, logger: Logger):
        self._state_path = state_path
        self._logger = logger
        self._state: dict[str, Any] = {}

    def create_fresh(self, run_id: str, max_retries: int, spec_hash: str) -> dict:
        self._state = {
            "run_id": run_id,
            "phase": PHASE_INIT,
            "attempt": 0,
            "max_retries": max_retries,
            "test_passed": False,
            "last_test_output": "",
            "attempt_files": [],
            "spec_hash": spec_hash,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._save()
        self._logger.info("state_created_fresh", {"run_id": run_id})
        return self._state.copy()

    def load_existing(self, spec_hash: str) -> dict | None:
        if not os.path.isfile(self._state_path):
            return None

        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                raw = f.read()
                if not raw.strip():
                    self._logger.warn("state_file_empty")
                    return None
                data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            self._logger.warn("state_load_failed", {"error": str(e)})
            return None

        if not isinstance(data, dict):
            self._logger.warn("state_not_dict", {"type": type(data).__name__})
            return None

        schema_errors = _validate_state_schema(data)
        if schema_errors:
            self._logger.warn("state_schema_invalid", {"errors": schema_errors})
            return None

        if data.get("spec_hash") != spec_hash:
            self._logger.warn(
                "state_spec_hash_mismatch",
                {
                    "stored": data.get("spec_hash"),
                    "current": spec_hash,
                },
            )
            return None

        if data.get("phase") in TERMINAL_PHASES:
            self._logger.info(
                "state_already_terminal",
                {"phase": data.get("phase")},
            )
            return None

        self._state = data
        self._logger.info(
            "state_resumed",
            {
                "run_id": data.get("run_id"),
                "phase": data.get("phase"),
                "attempt": data.get("attempt"),
            },
        )
        return self._state.copy()

    def get(self) -> dict:
        return self._state.copy()

    def update(self, **kwargs) -> dict:
        new_phase = kwargs.get("phase")
        if new_phase is not None:
            current_phase = self._state.get("phase")
            if current_phase and new_phase != current_phase:
                allowed = VALID_TRANSITIONS.get(current_phase, frozenset())
                if new_phase not in allowed:
                    self._logger.error(
                        "invalid_phase_transition",
                        {"from": current_phase, "to": new_phase, "allowed": sorted(allowed)},
                    )
                    raise ValueError(
                        f"Invalid state transition: {current_phase} -> {new_phase} "
                        f"(allowed: {sorted(allowed)})"
                    )
        for key, value in kwargs.items():
            self._state[key] = value
        self._state["updated_at"] = _now()
        self._save()
        self._logger.info(
            "state_updated",
            {k: v for k, v in kwargs.items() if k != "last_test_output"},
        )
        return self._state.copy()

    def _save(self):
        dir_name = os.path.dirname(self._state_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=dir_name or ".", suffix=".tmp", prefix="state_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._state_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

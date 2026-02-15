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

ALL_PHASES = {
    PHASE_INIT,
    PHASE_GENERATING,
    PHASE_GENERATED,
    PHASE_TESTING,
    PHASE_TESTED,
    PHASE_PATCHING,
    PHASE_SUCCESS,
    PHASE_FAILED,
}

TERMINAL_PHASES = {PHASE_SUCCESS, PHASE_FAILED}


def compute_spec_hash(spec_content: str) -> str:
    return "sha256:" + hashlib.sha256(spec_content.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        return self._state

    def load_existing(self, spec_hash: str) -> dict | None:
        if not os.path.isfile(self._state_path):
            return None

        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self._logger.warn("state_load_failed", {"error": str(e)})
            return None

        if self._state.get("spec_hash") != spec_hash:
            self._logger.warn(
                "state_spec_hash_mismatch",
                {
                    "stored": self._state.get("spec_hash"),
                    "current": spec_hash,
                },
            )
            return None

        if self._state.get("phase") in TERMINAL_PHASES:
            self._logger.info(
                "state_already_terminal",
                {"phase": self._state.get("phase")},
            )
            return None

        self._logger.info(
            "state_resumed",
            {
                "run_id": self._state.get("run_id"),
                "phase": self._state.get("phase"),
                "attempt": self._state.get("attempt"),
            },
        )
        return self._state

    def get(self) -> dict:
        return self._state

    def update(self, **kwargs) -> dict:
        for key, value in kwargs.items():
            self._state[key] = value
        self._state["updated_at"] = _now()
        self._save()
        self._logger.info(
            "state_updated",
            {k: v for k, v in kwargs.items() if k != "last_test_output"},
        )
        return self._state

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

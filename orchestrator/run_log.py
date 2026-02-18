import json
import os
import tempfile
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return str(uuid.uuid4())


class RunLog:
    def __init__(self, run_id: str, model_name: str, adapter_name: str, logs_path: str):
        self._logs_path = logs_path
        self._data = {
            "run_id": run_id,
            "start_timestamp": _now(),
            "end_timestamp": None,
            "model_name": model_name,
            "adapter_name": adapter_name,
            "state_history": [],
            "retry_count": 0,
            "tool_calls": [],
            "validation_failures": 0,
            "final_state": None,
        }

    def record_transition(self, from_phase: str, to_phase: str):
        self._data["state_history"].append(
            {"from": from_phase, "to": to_phase, "ts": _now()}
        )

    def record_tool_calls(self, tool_names: list[str]):
        self._data["tool_calls"].append(
            {"tools": tool_names, "ts": _now()}
        )

    def record_validation_failure(self):
        self._data["validation_failures"] += 1

    def set_retry_count(self, count: int):
        self._data["retry_count"] = count

    def finalize(self, final_state: str):
        self._data["end_timestamp"] = _now()
        self._data["final_state"] = final_state

    def write(self):
        try:
            os.makedirs(self._logs_path, exist_ok=True)
            out_path = os.path.join(
                self._logs_path, f"{self._data['run_id']}.json"
            )
            fd, tmp_path = tempfile.mkstemp(
                dir=self._logs_path, suffix=".tmp", prefix="runlog_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, default=str)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, out_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except OSError:
            pass

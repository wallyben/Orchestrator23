import json
import os
import sys
from datetime import datetime, timezone


class Logger:
    def __init__(self, logs_path: str, run_id: str):
        os.makedirs(logs_path, exist_ok=True)
        self._log_file_path = os.path.join(logs_path, f"{run_id}.log")
        self._run_id = run_id
        self._file = open(self._log_file_path, "a", encoding="utf-8")
        self.info("logger_initialized", {"log_file": self._log_file_path})

    def _write(self, level: str, event: str, data: dict | None = None):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "level": level,
            "event": event,
        }
        if data:
            entry["data"] = data
        line = json.dumps(entry, default=str)
        self._file.write(line + "\n")
        self._file.flush()
        tag = f"[{level.upper():5s}]"
        print(f"{tag} {event}", file=sys.stderr)

    def info(self, event: str, data: dict | None = None):
        self._write("info", event, data)

    def warn(self, event: str, data: dict | None = None):
        self._write("warn", event, data)

    def error(self, event: str, data: dict | None = None):
        self._write("error", event, data)

    def close(self):
        if self._file and not self._file.closed:
            self.info("logger_closed")
            self._file.close()

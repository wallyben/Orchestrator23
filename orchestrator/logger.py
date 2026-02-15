import json
import os
import sys
from datetime import datetime, timezone


class Logger:
    def __init__(self, logs_path: str, run_id: str):
        os.makedirs(logs_path, exist_ok=True)
        self._log_file_path = os.path.join(logs_path, f"{run_id}.log")
        self._run_id = run_id
        self._closed = False
        try:
            self._file = open(self._log_file_path, "a", encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Failed to open log file {self._log_file_path}: {e}", file=sys.stderr)
            self._file = None
            self._closed = True
            return
        self.info("logger_initialized", {"log_file": self._log_file_path})

    def _write(self, level: str, event: str, data: dict | None = None):
        tag = f"[{level.upper():5s}]"
        console_line = f"{tag} {event}"

        if self._closed or self._file is None:
            print(console_line, file=sys.stderr)
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "level": level,
            "event": event,
        }
        if data:
            entry["data"] = data

        try:
            line = json.dumps(entry, default=str)
            self._file.write(line + "\n")
            self._file.flush()
        except (OSError, ValueError, TypeError):
            pass

        print(console_line, file=sys.stderr)

    def info(self, event: str, data: dict | None = None):
        self._write("info", event, data)

    def warn(self, event: str, data: dict | None = None):
        self._write("warn", event, data)

    def error(self, event: str, data: dict | None = None):
        self._write("error", event, data)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._file is not None and not self._file.closed:
            try:
                self._write_raw("info", "logger_closed")
                self._file.close()
            except OSError:
                pass

    def _write_raw(self, level: str, event: str):
        if self._file is None or self._file.closed:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "level": level,
            "event": event,
        }
        try:
            self._file.write(json.dumps(entry, default=str) + "\n")
            self._file.flush()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

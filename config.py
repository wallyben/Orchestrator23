import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
SPEC_FILE = os.path.join(BASE_DIR, "spec.md")

MAX_RETRIES = 5
SUBPROCESS_TIMEOUT_SECONDS = 120

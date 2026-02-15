# Orchestrator23 — Pivot Architecture

## What This Is

A local deterministic build orchestrator that runs on a single laptop.

```
spec → generate code → run tests → patch → retry → stop
```

No cloud. No containers. No distributed systems. No frameworks.
Python only. CLI only. Deterministic. Bounded.

---

## Repository File Tree

```
Orchestrator23/
├── ARCHITECTURE.md            # This document
├── README.md                  # Usage instructions
├── orchestrator/              # All source code
│   ├── __init__.py
│   ├── cli.py                 # CLI entry point (argparse)
│   ├── engine.py              # Main orchestration loop
│   ├── state.py               # State machine + state.json I/O
│   ├── generator.py           # Code generation (calls LLM or template)
│   ├── runner.py              # Test execution (subprocess)
│   ├── patcher.py             # Applies patches from test failures
│   ├── safety.py              # Path validation, sandboxing checks
│   └── config.py              # Configuration loading + defaults
├── tests/                     # Tests for the orchestrator itself
│   ├── __init__.py
│   ├── test_engine.py
│   ├── test_state.py
│   ├── test_generator.py
│   ├── test_runner.py
│   ├── test_patcher.py
│   └── test_safety.py
├── workspace/                  # Generated code lands here (gitignored)
├── logs/                       # Run logs land here (gitignored)
├── state.json                  # Current run state (gitignored)
├── config.yaml                 # User configuration
├── .gitignore
└── pyproject.toml              # Project metadata + dependencies
```

### What Each Directory Is

| Path | Purpose | Gittracked |
|------|---------|------------|
| `orchestrator/` | All orchestrator source code | Yes |
| `tests/` | Unit tests for the orchestrator | Yes |
| `workspace/` | Isolated directory for all generated code | No |
| `logs/` | All run logs, one file per run | No |
| `state.json` | Current state of the orchestration run | No |
| `config.yaml` | User-editable configuration | Yes (template) |

### What Gets Deleted From Current Repo

Everything except `.git/` and this architecture. The repo is currently just a README. Nothing to remove.

---

## Architecture

### Component Responsibilities

```
cli.py          Parse args, load config, call engine.run()
                No business logic. No state. Just wiring.

engine.py       The single orchestration loop.
                Reads state. Decides next action. Calls generator/runner/patcher.
                Writes state after every step. This is the only component
                that drives the loop.

state.py        Owns state.json. Read/write/transition.
                Implements the state machine. Validates transitions.
                Handles crash recovery (state is always written atomically).

generator.py    Takes a spec (string) + optional prior failure context.
                Produces code files written to workspace/.
                Has no knowledge of retries or state.

runner.py       Executes tests against workspace/ code via subprocess.
                Returns structured result: pass/fail + stdout + stderr + exit code.
                Has no knowledge of retries or state.

patcher.py      Takes test failure output + current code.
                Produces a patched version of the code.
                Writes patched files back to workspace/.
                Has no knowledge of retries or state.

safety.py       Validates all file paths resolve inside workspace/.
                Validates no writes outside workspace/.
                Validates no self-modification of orchestrator/ files.
                Called by generator, runner, and patcher before any file I/O.

config.py       Loads config.yaml with defaults.
                Provides typed access to: max_retries, workspace_path,
                logs_path, test_command, generator_backend, etc.
```

### Data Flow

```
User
  │
  ▼
cli.py ──→ config.yaml ──→ config.py
  │
  ▼
engine.py
  │
  ├──→ state.py (read current state)
  │
  ├──→ generator.py (spec → code in workspace/)
  │       │
  │       └──→ safety.py (validate output paths)
  │
  ├──→ runner.py (run tests against workspace/)
  │       │
  │       └──→ safety.py (validate execution scope)
  │
  ├──→ patcher.py (failures → patched code)
  │       │
  │       └──→ safety.py (validate patch paths)
  │
  └──→ state.py (write updated state)
        │
        └──→ state.json (atomic write)
```

---

## State Machine

### States

```
INIT ──→ GENERATING ──→ TESTING ──→ PATCHING ──→ TESTING ──→ ... ──→ DONE
  │          │              │           │              │                 │
  │          ▼              ▼           ▼              ▼                 │
  │       FAILED         FAILED      FAILED        FAILED               │
  │          │              │           │              │                 │
  └──────────┴──────────────┴───────────┴──────────────┘                 │
             All failures check retry count.                             │
             If retries < max: go back to GENERATING.                    │
             If retries >= max: go to FAILED (terminal).                 │
                                                                         │
             DONE is terminal. Tests passed.◄────────────────────────────┘
```

### State Enum (exact values stored in state.json)

```
INIT        → Run has been created but nothing executed yet
GENERATING  → Code generation is in progress
TESTING     → Tests are being executed
PATCHING    → Patch is being applied based on test failures
DONE        → Tests passed. Terminal success state.
FAILED      → Max retries exceeded. Terminal failure state.
```

### Valid Transitions

```
INIT       → GENERATING
GENERATING → TESTING
GENERATING → FAILED      (generation itself errored, retry limit hit)
TESTING    → DONE         (tests passed)
TESTING    → PATCHING     (tests failed, retries remain)
TESTING    → FAILED       (tests failed, no retries remain)
PATCHING   → GENERATING   (patch applied, re-generate with context)
PATCHING   → FAILED       (patch itself errored, retry limit hit)
```

Any transition not in this list is illegal and causes an immediate hard stop.

### state.json Schema

```json
{
  "run_id": "uuid-v4",
  "status": "INIT|GENERATING|TESTING|PATCHING|DONE|FAILED",
  "spec": "the original user spec string",
  "retry_count": 0,
  "max_retries": 5,
  "history": [
    {
      "attempt": 1,
      "timestamp": "ISO-8601",
      "action": "generate|test|patch",
      "result": "success|failure",
      "detail": "string: error message or summary"
    }
  ],
  "last_test_output": "string or null",
  "last_error": "string or null",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

### Atomic State Writes

State is never written in-place. The write procedure is:

1. Serialize state to JSON.
2. Write to `state.json.tmp`.
3. `os.replace("state.json.tmp", "state.json")` — atomic on POSIX.

If the process crashes between steps 1-2, the old `state.json` is intact.
If the process crashes between steps 2-3, `state.json` is intact, `.tmp` is discarded on resume.

### Crash Recovery

On startup, `engine.py` checks:
1. Does `state.json` exist?
2. If yes: load it, validate schema, resume from current `status`.
3. If `state.json.tmp` exists but `state.json` does not: the last write failed mid-flight. This is treated as a corrupt state. Hard stop with error.
4. If neither exists: fresh run, create INIT state.

Resume behavior by state:
- `INIT` → restart from beginning (no work lost)
- `GENERATING` → generation was interrupted. Re-run generation from scratch.
- `TESTING` → tests were interrupted. Re-run tests.
- `PATCHING` → patching was interrupted. Re-run patching.
- `DONE` → nothing to do, report success.
- `FAILED` → nothing to do, report failure.

---

## Retry Loop Logic

### The Core Loop (engine.py pseudocode)

```
load state (or create INIT)
validate state

while True:
    if state.status == INIT:
        transition(GENERATING)

    if state.status == GENERATING:
        result = generator.generate(state.spec, state.last_test_output)
        if result.success:
            transition(TESTING)
        else:
            if state.retry_count >= state.max_retries:
                transition(FAILED)
                break
            state.retry_count += 1
            log(result.error)
            transition(GENERATING)  # retry generation
            continue

    if state.status == TESTING:
        result = runner.run_tests()
        if result.success:
            transition(DONE)
            break
        else:
            if state.retry_count >= state.max_retries:
                transition(FAILED)
                break
            state.last_test_output = result.output
            transition(PATCHING)

    if state.status == PATCHING:
        result = patcher.patch(state.last_test_output)
        if result.success:
            state.retry_count += 1
            transition(GENERATING)
        else:
            if state.retry_count >= state.max_retries:
                transition(FAILED)
                break
            state.retry_count += 1
            transition(GENERATING)  # even if patch fails, retry generation

    if state.status == DONE:
        break

    if state.status == FAILED:
        break
```

### Retry Counting Rules

- `retry_count` starts at 0.
- Incremented once per generate→test→patch cycle that does not end in DONE.
- The first attempt (`retry_count == 0`) is not a retry — it is the initial attempt.
- `max_retries` defaults to 3. Configurable via `config.yaml` and CLI flag `--max-retries`.
- When `retry_count >= max_retries`, the loop terminates with FAILED.
- This means total attempts = `max_retries + 1` (initial + retries).

### Timeout Per Step

Each subprocess call (generation, testing) has a hard timeout:
- `test_timeout`: default 120 seconds. Configurable.
- `generate_timeout`: default 300 seconds. Configurable.

If a subprocess exceeds its timeout, it is killed and counted as a failure.

---

## Failure Stop Conditions

The orchestrator enters terminal FAILED state and halts when ANY of these are true:

| # | Condition | Rationale |
|---|-----------|-----------|
| 1 | `retry_count >= max_retries` | Bounded retry exhausted |
| 2 | State transition is illegal | Logic bug or corruption |
| 3 | `state.json` fails schema validation on load | Corrupt state |
| 4 | Safety violation detected (path traversal, self-modify) | Security boundary breached |
| 5 | `workspace/` directory does not exist and cannot be created | Filesystem problem |
| 6 | Generator produces zero output files | Nothing to test |
| 7 | Test command is not found or not executable | Configuration error |
| 8 | Unhandled exception in engine loop | Catch-all, logged, hard stop |

On any terminal failure:
1. State is written as FAILED with `last_error` populated.
2. Full error is logged to `logs/`.
3. Process exits with non-zero exit code.
4. No cleanup of `workspace/` — user may want to inspect.

---

## Safety Boundaries

### 1. No Self-Overwrite

The orchestrator **must never modify its own source code**.

- `safety.py` maintains a deny-list of paths: the `orchestrator/` directory, `tests/`, `ARCHITECTURE.md`, `pyproject.toml`, `config.yaml`, and any file outside `workspace/`.
- Every file write from `generator.py` and `patcher.py` is routed through `safety.validate_write_path(path)`.
- If a write target resolves (after symlink resolution) to anything outside `workspace/`, the operation is blocked and the run fails immediately.

### 2. No Path Traversal

All paths are validated with this procedure:

```
1. Resolve the path: os.path.realpath(candidate_path)
2. Resolve the workspace: os.path.realpath(workspace_dir)
3. Assert resolved path starts with resolved workspace + os.sep
   (or equals resolved workspace exactly)
4. If assertion fails → HARD STOP
```

Symlinks are resolved before comparison. This prevents `../../etc/passwd` and symlink attacks.

### 3. No Network Access From Generated Code

The `runner.py` subprocess executor does NOT enforce network isolation (no Docker, no containers — per constraints). However:
- The test command is user-specified and runs user-controlled tests.
- This is a local tool for the user's own machine. The user is the trust boundary.
- Documentation will note that generated code runs with the user's permissions.

### 4. No Shell Injection

- All subprocess calls use `subprocess.run()` with list-form arguments (not `shell=True`).
- Exception: if the user explicitly provides a shell test command string in config, it runs via `shell=True` but this is the user's own command on their own machine.
- Generator and patcher output is never interpolated into shell commands.

### 5. Workspace Isolation

- `workspace/` is the ONLY directory where generated code is written.
- Before each run, the engine verifies `workspace/` exists (creates it if not).
- The engine does NOT delete `workspace/` automatically. The user controls cleanup.
- Generated files are written with standard user permissions (no chmod 777, no setuid).

### 6. State Integrity

- `state.json` is validated against the expected schema on every load.
- Unknown fields are rejected (strict parsing).
- If `run_id` doesn't match between CLI invocation and state file, hard stop (prevents accidentally resuming a different run).

---

## Configuration (config.yaml)

```yaml
max_retries: 3
test_command: "pytest workspace/"
test_timeout: 120
generate_timeout: 300
workspace_dir: "./workspace"
logs_dir: "./logs"
generator:
  backend: "claude"       # or "openai" or "local"
  model: "claude-sonnet-4-20250514"
  api_key_env: "ANTHROPIC_API_KEY"  # read from env var, never stored in config
```

All values have sane defaults. Config file is optional. CLI flags override config file values.

---

## CLI Interface

```
# Fresh run
orchestrator run --spec "Build a fibonacci module with tests" --max-retries 5

# Resume interrupted run
orchestrator resume

# Check status
orchestrator status

# Clean workspace
orchestrator clean
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | DONE — tests passed |
| 1 | FAILED — retries exhausted or hard stop |
| 2 | Usage error (bad args, missing config) |

---

## Logging

- One log file per run: `logs/{run_id}.log`
- Plain text, line-oriented, timestamped.
- Format: `{ISO-8601} [{level}] {component}: {message}`
- Levels: DEBUG, INFO, WARN, ERROR
- Log file is append-only during a run.
- Console output mirrors INFO+ level to stderr.
- stdout is reserved for structured output (status command).

---

## Dependencies

Minimal:
- Python 3.10+ (standard library only for core)
- `pyyaml` — config parsing
- `anthropic` or `openai` — LLM API client (for generator backend)

No other dependencies. No frameworks. No ORMs. No web servers.

---

## What This Repository Will NOT Contain

- No Dockerfile
- No docker-compose.yaml
- No CI/CD pipelines
- No Kubernetes manifests
- No Terraform
- No Airflow DAGs
- No Celery tasks
- No FastAPI/Flask/Django
- No database
- No message queues
- No agent frameworks
- No multi-agent anything
- No self-improvement loops
- No unbounded recursion
- No cloud deployment scripts

---

PIVOT ARCHITECTURE LOCKED

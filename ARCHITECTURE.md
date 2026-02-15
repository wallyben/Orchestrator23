# Orchestrator23 — Architecture

Minimal local deterministic build orchestrator.

**What it does:** spec → generate code → run tests → patch → retry → stop

**What it is not:** Not a multi-agent system. Not a cloud system. Not a framework. Not a product.

**Constraints:** Single laptop. Python only. No Docker. No frameworks. CLI only.

---

## 1. Repository Structure

```
Orchestrator23/
├── README.md
├── ARCHITECTURE.md
├── orchestrator/
│   ├── __init__.py
│   ├── cli.py                 # Entry point. Parses args, calls engine.
│   ├── engine.py              # The retry loop. Owns the state machine.
│   ├── state.py               # Load/save/validate state.json
│   ├── spec.py                # Read and validate spec files
│   ├── generate.py            # Call code generation (LLM or template)
│   ├── runner.py              # Execute tests in subprocess
│   ├── patcher.py             # Apply diffs/patches to workspace files
│   └── safety.py              # Path validation, boundary enforcement
├── workspace/                 # Generated code lands here. Ephemeral.
├── logs/                      # One log file per run. Append-only.
├── specs/                     # User-provided spec files (input)
├── state.json                 # Single source of truth for run state
├── pyproject.toml             # Project metadata, no framework deps
└── tests/
    ├── __init__.py
    ├── test_engine.py
    ├── test_state.py
    ├── test_safety.py
    └── test_runner.py
```

No other directories. No `src/`. No `lib/`. No `utils/`. No `config/`. Flat module under `orchestrator/`.

---

## 2. State Machine

Six states. No ambiguity. No parallel paths.

```
INIT → GENERATING → TESTING → PATCHING → SUCCESS
                                  │
                                  └──→ FAILED
```

### State definitions

| State | Meaning | Next valid states |
|---|---|---|
| `INIT` | Spec loaded, workspace clean, ready to begin | `GENERATING` |
| `GENERATING` | Code generation in progress | `TESTING`, `FAILED` |
| `TESTING` | Running test suite against workspace | `SUCCESS`, `PATCHING`, `FAILED` |
| `PATCHING` | Applying fix based on test failure output | `TESTING`, `FAILED` |
| `SUCCESS` | All tests passed. Terminal state. | _(none)_ |
| `FAILED` | Max retries exhausted or unrecoverable error. Terminal state. | _(none)_ |

### State transitions (exhaustive)

```
INIT        → GENERATING     : always (start of run)
GENERATING  → TESTING        : generation succeeded
GENERATING  → FAILED         : generation produced no output or errored
TESTING     → SUCCESS        : all tests pass (exit code 0)
TESTING     → PATCHING       : tests fail AND retry_count < max_retries
TESTING     → FAILED         : tests fail AND retry_count >= max_retries
PATCHING    → TESTING        : patch applied, re-run tests (retry_count++)
PATCHING    → FAILED         : patch operation itself errors
```

No other transitions exist. Any state not in this table is a bug.

### `state.json` schema

```json
{
  "run_id": "uuid4",
  "spec_file": "specs/example.yaml",
  "state": "TESTING",
  "retry_count": 2,
  "max_retries": 5,
  "last_test_exit_code": 1,
  "last_test_stderr": "AssertionError: expected 4 got 5",
  "last_error": null,
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

Written atomically: write to `state.json.tmp`, then `os.replace()` to `state.json`. This guarantees crash safety — the file is always valid or absent.

---

## 3. Retry Loop Logic

The engine runs exactly this loop. No deviation.

```
function run(spec_file, max_retries):
    state = load_or_init(spec_file, max_retries)

    if state.state == SUCCESS or state.state == FAILED:
        print result and exit

    if state.state == INIT:
        transition(GENERATING)
        result = generate(spec)
        if result failed:
            transition(FAILED)
            stop
        write files to workspace/
        transition(TESTING)

    if state.state == PATCHING:
        # resumed after crash during PATCHING
        transition(TESTING)

    while state.state == TESTING:
        exit_code, stdout, stderr = run_tests(workspace/)
        if exit_code == 0:
            transition(SUCCESS)
            stop

        if state.retry_count >= state.max_retries:
            transition(FAILED)
            stop

        transition(PATCHING)
        patch_result = patch(workspace/, stderr)
        if patch_result failed:
            transition(FAILED)
            stop

        state.retry_count += 1
        transition(TESTING)

    stop
```

### Key properties

- **Bounded**: The loop runs at most `max_retries` iterations. Default: 5. Hard ceiling: 50.
- **Deterministic**: Same spec + same generation output = same loop behavior.
- **Resumable**: On crash, reload `state.json`, re-enter the loop at the current state. The `PATCHING` state resumes as `TESTING` (re-run tests to check if the patch landed).
- **No backoff**: Retries are immediate. This is local execution, not a network call.
- **No concurrency**: Single-threaded, single-process orchestrator. Subprocesses for test execution only.

---

## 4. Safety Boundaries

### Filesystem containment

1. **All generated/patched files MUST reside under `workspace/`.** Every file write in `generate.py` and `patcher.py` passes through `safety.resolve_path(target)`. This function computes `os.path.realpath()` and asserts the result starts with the absolute path of `workspace/`. If not, raise `SafetyViolation`. No exceptions. No overrides. No flags.

2. **`workspace/` is the only writable directory for generated content.** `state.json` is writable by the engine (root of repo). `logs/` is writable by the engine (append-only log files). Everything else is read-only from the orchestrator's perspective.

3. **No symlink following.** `os.path.realpath()` resolves symlinks before checking the boundary. A symlink inside `workspace/` pointing outside it will be caught and rejected.

### Subprocess containment

4. **Test execution runs via `subprocess.run()` with:**
   - `cwd=workspace/`
   - `timeout=300` (5 minutes, configurable, hard max 600 seconds)
   - `shell=False` (command passed as list, no shell injection)
   - stdout and stderr captured, not streamed to terminal
   - No `env` inheritance beyond a minimal allowlist: `PATH`, `HOME`, `LANG`, `PYTHONPATH` (set to workspace)

5. **No network calls from the orchestrator itself.** The `generate.py` module may call an LLM API — that is its only permitted external contact. The test runner, patcher, and engine make zero network calls.

### Input validation

6. **Spec files must be valid YAML or JSON.** Schema-validated on load. Unknown fields rejected.
7. **`max_retries` clamped to [1, 50].** CLI input outside this range is clamped with a warning.
8. **`state.json` validated on load.** If it contains an unknown state or fails schema validation, the run is marked `FAILED` with `last_error` explaining the corruption.

---

## 5. Stop Conditions

The orchestrator halts when **any** of these are true:

| # | Condition | Resulting state | Exit code |
|---|---|---|---|
| 1 | All tests pass (exit code 0) | `SUCCESS` | 0 |
| 2 | `retry_count >= max_retries` after a test failure | `FAILED` | 1 |
| 3 | Code generation produces no output | `FAILED` | 1 |
| 4 | Code generation errors (exception/crash) | `FAILED` | 1 |
| 5 | Patch operation errors (exception/crash) | `FAILED` | 1 |
| 6 | Safety violation detected (path escape) | `FAILED` | 2 |
| 7 | `state.json` corrupted on resume | `FAILED` | 3 |
| 8 | Test subprocess exceeds timeout | `FAILED` | 1 |
| 9 | Keyboard interrupt (SIGINT) | State preserved as-is | 130 |

There is no "maybe keep going" path. Every condition either advances the loop or terminates it. The orchestrator never hangs, never waits for input mid-run, and never retries without decrementing the remaining budget.

### On SIGINT (Ctrl+C)

- Current `state.json` is already persisted (written before each phase).
- The orchestrator exits immediately with code 130.
- Next invocation resumes from the last persisted state.

---

## 6. CLI Interface

```
python -m orchestrator run --spec specs/example.yaml --max-retries 5
python -m orchestrator status
python -m orchestrator reset
```

Three commands. No subcommand tree. No plugins.

- **`run`**: Execute the loop. Resumes if `state.json` exists and is not terminal.
- **`status`**: Print current `state.json` contents.
- **`reset`**: Delete `state.json` and clear `workspace/`. Does not touch `logs/` or `specs/`.

---

ARCHITECTURE LOCKED

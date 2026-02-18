# Orchestrator23 — Architecture

## 1. File Tree

```
/orchestrator
├── main.py              # CLI entry point, retry loop owner
├── claude_client.py     # LLM interface — sends prompts, receives code/patches
├── test_runner.py       # Runs pytest against /workspace, implements Tool protocol
├── tool_registry.py     # Tool protocol, ToolResult, and ToolRegistry
├── state_manager.py     # Reads/writes state.json, handles resume
├── logger.py            # Structured logging to /logs/<run_id>.log
├── config.py            # Static config: max retries, paths, model params
├── spec.md              # Product specification (user-provided input)
├── state.json           # Persistent state (created at runtime, example committed)
├── /workspace           # Generated project files land here
│   └── (generated)
└── /logs                # One log file per run
    └── (generated)
```

No nested packages. No `__init__.py`. Eight Python files. Flat.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────┐
│                     main.py                         │
│              (CLI + Retry Loop Owner)               │
│                                                     │
│   Reads spec.md                                     │
│   Initializes or resumes state                      │
│   Owns the generate → test → patch cycle            │
│   Enforces max retry hard stop                      │
└────────┬──────────┬──────────┬──────────┬───────────┘
         │          │          │          │
         ▼          ▼          ▼          ▼
   claude_client  tool_registry  state_mgr  logger
   .py            .py            .py        .py
                    │
                test_runner.py
```

**Dependency graph (strictly one-directional):**

```
main.py
 ├── config.py          (imported by all, depends on nothing)
 ├── logger.py          (imported by all except config)
 ├── state_manager.py   (imports config, logger)
 ├── claude_client.py   (imports config, logger)
 ├── tool_registry.py   (imports logger)
 └── test_runner.py     (imports config, logger, tool_registry)
```

No circular imports. No abstract base classes.

### Module Responsibilities

| Module | Single Responsibility |
|---|---|
| `config.py` | Constants and CLI arg parsing. Reads env vars for API key. Defines paths, max retries, model name. Returns a frozen dataclass. |
| `logger.py` | Creates a per-run log file at `/logs/<run_id>.log`. Provides `log(level, message, data)`. Writes JSON lines. Timestamps are UTC ISO-8601. |
| `state_manager.py` | Loads `state.json` on start. Writes after every state transition. Provides `get_state()`, `update_state()`, `mark_complete()`, `mark_failed()`. State file is the crash-recovery mechanism. |
| `claude_client.py` | Builds prompts. Calls the Anthropic API (via `anthropic` Python SDK). Two entry points: `generate_project(spec)` returns file contents, `generate_patch(spec, files, test_output)` returns file diffs/replacements. Parses structured output into a dict of `{filepath: content}`. |
| `tool_registry.py` | Defines the `Tool` protocol (`name` property + `run()` → `ToolResult`), the `ToolResult` frozen dataclass, and the `ToolRegistry` class. Registry runs all tools in registration order, catches exceptions per-tool, and returns a list of `ToolResult`. |
| `test_runner.py` | Runs `pytest /workspace` as a subprocess. Implements the `Tool` protocol (name=`"pytest"`). Captures stdout+stderr. Timeout-kills after configurable seconds. |
| `main.py` | CLI entry. Parses args. Creates `ToolRegistry`, registers tools, orchestrates the generate → run_all → evaluate loop. |

---

## 3. Execution Flow

```
START
  │
  ▼
[1] Parse CLI args (--spec, --max-retries, --resume)
  │
  ▼
[2] Load config.py → frozen Config dataclass
  │
  ▼
[3] Initialize logger (run_id = timestamp or resumed run_id)
  │
  ▼
[4] Load state.json via state_manager
  │
  ├── state.json missing or --no-resume → FRESH RUN
  │     state = { phase: "init", attempt: 0, run_id: <new> }
  │
  └── state.json exists + --resume → RESUME
        state = loaded from disk
        jump to the phase recorded in state
  │
  ▼
[5] Read spec.md into memory (raw string)
  │
  ▼
[6] ┌──────────── RETRY LOOP ────────────┐
  │  │                                    │
  │  │  if attempt > max_retries:         │
  │  │      → HARD STOP (exit 1)         │
  │  │                                    │
  │  │  [6a] GENERATE / PATCH            │
  │  │   ├─ attempt == 0:                │
  │  │   │   call claude_client           │
  │  │   │     .generate_project(spec)    │
  │  │   │   write files to /workspace    │
  │  │   │                                │
  │  │   └─ attempt > 0:                 │
  │  │       call claude_client           │
  │  │         .generate_patch(           │
  │  │            spec,                   │
  │  │            current_workspace_files,│
  │  │            last_test_output        │
  │  │         )                          │
  │  │       apply returned files to      │
  │  │         /workspace                 │
  │  │                                    │
  │  │  state → { phase: "generated",    │
  │  │            attempt: N }            │
  │  │  write state.json                  │
  │  │                                    │
  │  │  [6b] TEST                        │
  │  │   run test_runner.run_tests()      │
  │  │   capture (passed, output, rc)     │
  │  │                                    │
  │  │  state → { phase: "tested",       │
  │  │            test_passed: bool,      │
  │  │            last_test_output: str } │
  │  │  write state.json                  │
  │  │                                    │
  │  │  [6c] EVALUATE                    │
  │  │   if passed:                       │
  │  │       state → { phase: "complete" }│
  │  │       → EXIT SUCCESS (exit 0)     │
  │  │   else:                            │
  │  │       attempt += 1                 │
  │  │       loop back to [6a]            │
  │  │                                    │
  │  └────────────────────────────────────┘
  │
  ▼
[7] Final state written. Log closed. Process exits.
```

---

## 4. Retry Loop Logic — Precise Definition

```
function run_loop(spec, config, state):

    while state.attempt <= config.max_retries:

        log("attempt", state.attempt)

        # STEP A: Generate or Patch
        if state.phase in ("init", "tested"):
            if state.attempt == 0:
                files = claude_client.generate_project(spec)
            else:
                workspace_files = read_all_files("/workspace")
                files = claude_client.generate_patch(
                    spec, workspace_files, state.last_test_output
                )
            write_files_to_workspace(files)
            state.phase = "generated"
            state.attempt_files = list(files.keys())
            state_manager.save(state)

        # STEP B: Test
        if state.phase == "generated":
            passed, output, rc = test_runner.run_tests()
            state.phase = "tested"
            state.test_passed = passed
            state.last_test_output = output
            state_manager.save(state)

        # STEP C: Evaluate
        if state.phase == "tested":
            if state.test_passed:
                state.phase = "complete"
                state_manager.save(state)
                return SUCCESS

            state.attempt += 1
            # loop continues

    # Exhausted retries
    state.phase = "failed"
    state_manager.save(state)
    return FAILURE
```

**Key properties:**

- State is saved *between* every sub-step (generate, test, evaluate). A crash at any point resumes from the last completed sub-step.
- `attempt 0` = initial generation. `attempt 1..N` = patch attempts. So `max_retries=3` means 1 generation + 3 patches = 4 total LLM calls maximum.
- The test output from attempt N is fed as context to the LLM for attempt N+1. The LLM always sees: spec + current files + failure output.
- No exponential backoff. No jitter. No parallelism. Deterministic sequential execution.

---

## 5. State Machine Definition

```
States:
  INIT → GENERATED → TESTED → COMPLETE
                       ↓
                    (fail) → loops back to GENERATED (attempt++)
                       ↓
                    (max retries exceeded) → FAILED

Transitions:
  INIT        → GENERATED    : LLM generates initial project files
  GENERATED   → TESTED       : pytest runs against /workspace
  TESTED      → COMPLETE     : tests passed
  TESTED      → GENERATED    : tests failed, attempt < max, LLM patches
  TESTED      → FAILED       : tests failed, attempt >= max

Terminal States:
  COMPLETE    : exit 0
  FAILED      : exit 1
```

**`state.json` schema:**

```json
{
  "run_id": "20260215T143022Z",
  "phase": "tested",
  "attempt": 2,
  "max_retries": 5,
  "test_passed": false,
  "last_test_output": "FAILED test_auth.py::test_login - AssertionError...",
  "attempt_files": ["app.py", "auth.py", "test_auth.py"],
  "spec_hash": "sha256:a1b2c3...",
  "created_at": "2026-02-15T14:30:22Z",
  "updated_at": "2026-02-15T14:31:47Z"
}
```

- `spec_hash` ensures resume only works against the same spec. If spec changes, force fresh run.
- `attempt_files` tracks which files were written/modified in the last attempt for debugging.
- `phase` is the state machine position. On resume, execution jumps directly to the handler for that phase.

---

## Engineering Decisions

| Decision | Rationale |
|---|---|
| `anthropic` Python SDK for LLM calls | Direct, no wrappers. Single dependency beyond stdlib. |
| `subprocess.run` for pytest | No pytest API import contamination. Clean process boundary. Captures output as string. |
| JSON lines for logs | Grep-friendly. No log rotation needed for single-run tool. |
| Frozen dataclass for config | Immutable after parse. No config drift mid-run. |
| `spec_hash` in state | Prevents resuming stale state against a modified spec. |
| Files returned as `dict[str, str]` from LLM | Simplest possible representation. Path → content. No AST, no diff format. Full file replacement per attempt. |
| No diff/patch format from LLM | Full file replacement is more reliable than asking an LLM to produce valid unified diffs. Costs more tokens but eliminates patch-application failures. |
| Pytest as the only test runner | Spec says "runs tests." Pytest is the standard. No abstraction layer for "maybe mocha someday." |
| Test timeout via `subprocess.run(timeout=)` | Prevents infinite loops in generated code from hanging the orchestrator. |
| Single log file per run, not per attempt | One file to tail. Attempts are separated by log entries, not file boundaries. |

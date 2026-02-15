#!/usr/bin/env python3
"""Claude API client for Orchestrator23.

Interface:
    result = generate(spec, workspace_dir)
    result = patch(spec, test_stderr, workspace_dir)

    result.ok       -> bool
    result.files    -> dict[str, str]   (relative_path → content)
    result.error    -> str | None
    result.usage    -> dict | None
"""

import json
import os
import re
import time


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_MAX_TOKENS = 16384
API_TIMEOUT = 120
MAX_API_RETRIES = 3
BACKOFF_BASE = 2
STDERR_CAP = 8000


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class ClientResult:
    __slots__ = ("ok", "files", "error", "usage", "retries_used", "raw_response")

    def __init__(self, *, ok, files=None, error=None, usage=None,
                 retries_used=0, raw_response=None):
        self.ok = ok
        self.files = files or {}
        self.error = error
        self.usage = usage
        self.retries_used = retries_used
        self.raw_response = raw_response

    def __bool__(self):
        return self.ok and len(self.files) > 0

    def __repr__(self):
        if self.ok:
            return f"<ClientResult ok files={list(self.files.keys())}>"
        return f"<ClientResult FAIL error={self.error!r}>"


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------

def _make_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, "ANTHROPIC_API_KEY environment variable is not set"
    try:
        import anthropic
    except ImportError:
        return None, "anthropic package is not installed (pip install anthropic)"
    try:
        client = anthropic.Anthropic(api_key=key, timeout=API_TIMEOUT)
    except Exception as exc:
        return None, f"Failed to initialize Anthropic client: {exc}"
    return client, None


def _call(client, *, system, user, model, max_tokens):
    """Send a single messages request with exponential-backoff retry.

    Returns (message, retries_used) or raises on exhaustion.
    """
    try:
        import anthropic as _anthropic
        retryable = (
            _anthropic.APITimeoutError,
            _anthropic.RateLimitError,
            _anthropic.InternalServerError,
            _anthropic.APIConnectionError,
        )
    except Exception:
        retryable = ()

    last_exc = None
    for attempt in range(MAX_API_RETRIES + 1):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return message, attempt
        except retryable as exc:
            last_exc = exc
            if attempt < MAX_API_RETRIES:
                time.sleep(BACKOFF_BASE ** (attempt + 1))
        except Exception as exc:
            raise exc

    raise last_exc


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_json(text):
    """Best-effort extraction of a JSON object from model output."""
    stripped = text.strip()

    # 1) raw parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2) fenced code block
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", stripped, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3) first { … last }
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(stripped[first : last + 1])
        except json.JSONDecodeError:
            pass

    return None


def _parse_files(message):
    """Parse a Claude response into a files dict.

    Returns (files_dict, usage_dict, error_string).
    error_string is None on success.
    """
    usage = None
    if hasattr(message, "usage") and message.usage:
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }

    if not message.content:
        return None, usage, "API returned empty content"

    text = ""
    for block in message.content:
        if hasattr(block, "text"):
            text += block.text

    if not text.strip():
        return None, usage, "API returned blank text"

    # Truncation check
    if message.stop_reason == "max_tokens":
        return None, usage, (
            f"Response truncated at max_tokens "
            f"({message.usage.output_tokens} tokens produced). "
            f"Increase max_tokens in spec or simplify the request."
        )

    if message.stop_reason not in ("end_turn", "stop_sequence"):
        return None, usage, f"Unexpected stop_reason: {message.stop_reason}"

    data = _extract_json(text)
    if data is None:
        preview = text[:200].replace("\n", "\\n")
        return None, usage, (
            f"Cannot extract JSON from response (length={len(text)}). "
            f"Preview: {preview}"
        )

    if not isinstance(data, dict):
        return None, usage, f"JSON root is {type(data).__name__}, expected object"

    # Accept {"files": {…}} or a flat {path: content} mapping
    files = data.get("files", data)
    if not isinstance(files, dict):
        return None, usage, "'files' value is not a mapping"

    bad_keys = [k for k, v in files.items() if not isinstance(k, str) or not isinstance(v, str)]
    if bad_keys:
        return None, usage, f"Non-string entries in files: {bad_keys[:5]}"

    if len(files) == 0:
        return None, usage, "Response JSON contained zero files"

    return files, usage, None


# ---------------------------------------------------------------------------
# Workspace reader  (for patch context)
# ---------------------------------------------------------------------------

def _read_workspace(workspace_dir):
    """Read all text files from workspace/ into {relative_path: content}."""
    result = {}
    if not os.path.isdir(workspace_dir):
        return result
    for dirpath, _, filenames in os.walk(workspace_dir):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, workspace_dir)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    result[rel_path] = fh.read()
            except OSError:
                continue
    return result


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GENERATE_SYSTEM = (
    "You are a code generator. You receive a specification and produce source files.\n"
    "\n"
    "Return ONLY a JSON object with this exact structure:\n"
    '{"files": {"relative/path/file.ext": "file content...", ...}}\n'
    "\n"
    "Rules:\n"
    "- Every value must be the COMPLETE file content. Never truncate.\n"
    "- Paths are relative to the project root.\n"
    "- Do not include explanation, markdown fences, or anything outside the JSON.\n"
    "- Produce fully working code that will pass the test command.\n"
)

PATCH_SYSTEM = (
    "You are a code patcher. You receive current source files and test failure output.\n"
    "Your job is to fix the code so the tests pass.\n"
    "\n"
    "Return ONLY a JSON object with this exact structure:\n"
    '{"files": {"relative/path/file.ext": "full corrected file content...", ...}}\n'
    "\n"
    "Rules:\n"
    "- Include ONLY files that need changes.\n"
    "- Each value must be the COMPLETE corrected file content (not a diff).\n"
    "- Do not include explanation, markdown fences, or anything outside the JSON.\n"
    "- Do not truncate any file.\n"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(spec, workspace_dir):
    """Generate code from a spec.

    Returns a ClientResult.  Never raises.
    """
    client, client_err = _make_client()
    if client_err:
        return ClientResult(ok=False, error=client_err)

    model = spec.get("model", os.environ.get("ORCHESTRATOR_MODEL", DEFAULT_MODEL))
    max_tokens = int(spec.get("max_tokens", DEFAULT_MAX_TOKENS))

    parts = []
    if spec.get("description"):
        parts.append(f"## Description\n{spec['description']}")
    if spec.get("requirements"):
        parts.append(f"## Requirements\n{spec['requirements']}")
    if spec.get("test_command"):
        cmd = spec["test_command"]
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        parts.append(f"## Test command (code must pass this)\n{cmd}")
    if spec.get("files"):
        parts.append(f"## Expected file structure\n{json.dumps(spec['files'], indent=2)}")

    known_keys = {
        "description", "requirements", "test_command", "test_timeout",
        "files", "model", "max_tokens",
    }
    extra = {k: v for k, v in spec.items() if k not in known_keys}
    if extra:
        parts.append(f"## Additional context\n{json.dumps(extra, indent=2)}")

    user_prompt = "\n\n".join(parts) if parts else json.dumps(spec, indent=2)

    try:
        message, retries = _call(
            client, system=GENERATE_SYSTEM, user=user_prompt,
            model=model, max_tokens=max_tokens,
        )
    except Exception as exc:
        return ClientResult(
            ok=False,
            error=f"API call failed after {MAX_API_RETRIES} retries: {exc}",
            retries_used=MAX_API_RETRIES,
        )

    files, usage, parse_err = _parse_files(message)
    if parse_err:
        raw = ""
        if message.content:
            raw = message.content[0].text if hasattr(message.content[0], "text") else ""
        return ClientResult(
            ok=False, error=parse_err, usage=usage,
            raw_response=raw, retries_used=retries,
        )

    return ClientResult(ok=True, files=files, usage=usage, retries_used=retries)


def patch(spec, test_stderr, workspace_dir):
    """Patch workspace code to fix test failures.

    Returns a ClientResult.  Never raises.
    """
    client, client_err = _make_client()
    if client_err:
        return ClientResult(ok=False, error=client_err)

    model = spec.get("model", os.environ.get("ORCHESTRATOR_MODEL", DEFAULT_MODEL))
    max_tokens = int(spec.get("max_tokens", DEFAULT_MAX_TOKENS))

    current_files = _read_workspace(workspace_dir)
    if not current_files:
        return ClientResult(ok=False, error="No files found in workspace to patch")

    parts = []
    if spec.get("description"):
        parts.append(f"## Specification\n{spec['description']}")

    parts.append(f"## Current source files\n{json.dumps(current_files, indent=2)}")

    stderr_trimmed = (test_stderr or "")[:STDERR_CAP]
    parts.append(f"## Test failure output\n{stderr_trimmed}")

    if spec.get("test_command"):
        cmd = spec["test_command"]
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        parts.append(f"## Test command\n{cmd}")

    user_prompt = "\n\n".join(parts)

    try:
        message, retries = _call(
            client, system=PATCH_SYSTEM, user=user_prompt,
            model=model, max_tokens=max_tokens,
        )
    except Exception as exc:
        return ClientResult(
            ok=False,
            error=f"API call failed after {MAX_API_RETRIES} retries: {exc}",
            retries_used=MAX_API_RETRIES,
        )

    files, usage, parse_err = _parse_files(message)
    if parse_err:
        raw = ""
        if message.content:
            raw = message.content[0].text if hasattr(message.content[0], "text") else ""
        return ClientResult(
            ok=False, error=parse_err, usage=usage,
            raw_response=raw, retries_used=retries,
        )

    return ClientResult(ok=True, files=files, usage=usage, retries_used=retries)

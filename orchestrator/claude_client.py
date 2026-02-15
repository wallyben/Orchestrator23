import json
import re
import time

import anthropic

from config import Config
from logger import Logger

FILE_BLOCK_PATTERN = re.compile(
    r"===== FILE:\s*(.+?)\s*=====\n(.*?)(?=\n===== FILE:|\n===== END =====|$)",
    re.DOTALL,
)

SYSTEM_PROMPT = """\
You are a senior software engineer. You generate complete, production-ready Python project files.

RESPONSE FORMAT — you MUST use this exact format for every file you output:

===== FILE: <relative_path> =====
<full file content>
===== END =====

Rules:
- Output EVERY file needed for the project to work, including test files.
- Test files MUST be named with a test_ prefix (e.g., test_main.py) so pytest discovers them.
- All test files must use pytest (import pytest where needed).
- Write complete files. No placeholders. No TODOs. No truncation.
- Do not wrap file contents in markdown code fences.
- Relative paths only (e.g., app.py, utils/helpers.py, test_app.py).
- Include a requirements.txt if any non-stdlib packages are needed (exclude pytest itself).
- Every function and class in source files must be covered by at least one test.
"""

GENERATE_PROMPT_TEMPLATE = """\
Generate a complete Python project that satisfies this specification:

--- SPECIFICATION ---
{spec}
--- END SPECIFICATION ---

Generate ALL source files AND test files. Tests must pass when run with pytest.
"""

PATCH_PROMPT_TEMPLATE = """\
The project below was generated from the specification but tests are FAILING.

--- SPECIFICATION ---
{spec}
--- END SPECIFICATION ---

--- CURRENT FILES ---
{files_block}
--- END CURRENT FILES ---

--- TEST FAILURE OUTPUT ---
{test_output}
--- END TEST FAILURE OUTPUT ---

Fix the failing tests. Output ALL project files (not just the changed ones) using the required format.
Every file must be complete. Do not skip unchanged files.
"""


def _format_files_block(files: dict[str, str]) -> str:
    parts = []
    for path, content in sorted(files.items()):
        parts.append(f"===== FILE: {path} =====\n{content}")
    parts.append("===== END =====")
    return "\n".join(parts)


def _parse_files(text: str) -> dict[str, str]:
    files = {}
    for match in FILE_BLOCK_PATTERN.finditer(text):
        filepath = match.group(1).strip()
        content = match.group(2)
        if content.endswith("\n===== END"):
            content = content[: -len("\n===== END")]
        content = content.strip("\n") + "\n"
        filepath = filepath.lstrip("/").lstrip("./")
        if filepath:
            files[filepath] = content
    return files


class ClaudeClient:
    def __init__(self, config: Config, logger: Logger):
        self._config = config
        self._logger = logger
        self._client = anthropic.Anthropic(
            api_key=config.api_key,
            timeout=float(config.api_timeout),
        )

    def generate_project(self, spec: str) -> dict[str, str]:
        prompt = GENERATE_PROMPT_TEMPLATE.format(spec=spec)
        return self._call(prompt, "generate_project")

    def generate_patch(
        self, spec: str, current_files: dict[str, str], test_output: str
    ) -> dict[str, str]:
        files_block = _format_files_block(current_files)
        truncated_output = test_output[:15000]
        prompt = PATCH_PROMPT_TEMPLATE.format(
            spec=spec, files_block=files_block, test_output=truncated_output
        )
        return self._call(prompt, "generate_patch")

    def _call(self, user_prompt: str, operation: str) -> dict[str, str]:
        self._logger.info(
            f"claude_api_call_start",
            {"operation": operation, "prompt_length": len(user_prompt)},
        )

        start = time.monotonic()

        try:
            response = self._client.messages.create(
                model=self._config.model_name,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APITimeoutError:
            self._logger.error("claude_api_timeout", {"operation": operation})
            raise RuntimeError(f"Claude API timed out after {self._config.api_timeout}s")
        except anthropic.APIError as e:
            self._logger.error(
                "claude_api_error",
                {"operation": operation, "error": str(e), "status": getattr(e, "status_code", None)},
            )
            raise RuntimeError(f"Claude API error: {e}")

        elapsed = time.monotonic() - start

        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        self._logger.info(
            "claude_api_call_done",
            {
                "operation": operation,
                "elapsed_s": round(elapsed, 2),
                "response_length": len(raw_text),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "stop_reason": response.stop_reason,
            },
        )

        files = _parse_files(raw_text)

        if not files:
            self._logger.error(
                "claude_parse_no_files",
                {"operation": operation, "raw_text_head": raw_text[:500]},
            )
            raise RuntimeError(
                "Claude returned a response but no files could be parsed. "
                "The model may not have followed the expected output format."
            )

        self._logger.info(
            "claude_files_parsed",
            {"operation": operation, "file_count": len(files), "files": list(files.keys())},
        )

        return files

import json
import os
import re
from pathlib import Path

from .base import BaseLLMProvider


class OpenAIProvider(BaseLLMProvider):
    """LLM provider backed by OpenAI's chat-completions API (default: gpt-4o)."""

    def __init__(self, api_key: str = None, model: str = "gpt-4o"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model

    def patch_from_prompt(self, prompt_text: str) -> dict:
        """Call OpenAI with workspace context and return a patch dict.

        Workflow:
        1. Build workspace context via get_workspace_context(WORKSPACE_DIR).
        2. Send a system + user message to the model.
        3. Log the raw response text to logs/last_llm_raw.txt.
        4. Strip any markdown code fences from the response.
        5. Parse and return the JSON dict (no file writes here).

        Returns:
            dict with key "files" – see BaseLLMProvider.patch_from_prompt.

        Raises:
            json.JSONDecodeError: if the model response cannot be parsed as JSON
                after fence-stripping.
            openai.OpenAIError: on API-level failures.
        """
        import openai  # lazy import – openai is optional at import time

        from config import LOGS_DIR, WORKSPACE_DIR  # absolute paths from repo root

        # --- build context ---------------------------------------------------
        context = self.get_workspace_context(WORKSPACE_DIR)

        system_msg = (
            "You are a code-patching assistant. "
            "Given the current workspace files and a task description, "
            "return ONLY a JSON object (no markdown, no commentary) with a "
            "single top-level key \"files\". Each entry under \"files\" must "
            "have \"path\" (relative to workspace/) and \"content\" (the full "
            "updated file content as a plain string). "
            "Example: "
            "{\"files\": [{\"path\": \"app/foo.py\", \"content\": \"def foo(): pass\\n\"}]}"
        )

        user_msg = (
            f"Current workspace files:\n\n{context}\n\n"
            f"Task: {prompt_text}"
        )

        # --- call API --------------------------------------------------------
        client = openai.OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        raw: str = response.choices[0].message.content or ""

        # --- log raw output --------------------------------------------------
        logs_path = Path(LOGS_DIR)
        logs_path.mkdir(parents=True, exist_ok=True)
        (logs_path / "last_llm_raw.txt").write_text(raw, encoding="utf-8")

        # --- strict JSON parsing with fence-stripping ------------------------
        # Remove ```json ... ``` or ``` ... ``` wrappers if present
        clean = re.sub(
            r"```(?:json)?\s*\n?(.*?)```",
            r"\1",
            raw,
            flags=re.DOTALL,
        ).strip()

        return json.loads(clean)

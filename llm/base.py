from abc import ABC, abstractmethod
from pathlib import Path


class BaseLLMProvider(ABC):
    """Abstract base for LLM providers used by the job orchestrator."""

    def get_workspace_context(self, workspace_dir: str) -> str:
        """Return a concatenated view of all Python files in workspace_dir.

        Reads every *.py file found recursively and formats them as:
            ### <relative-path>
            <file content>

        Returns an empty string if workspace_dir does not exist.
        """
        workspace_path = Path(workspace_dir)
        if not workspace_path.exists():
            return ""

        parts = []
        for py_file in sorted(workspace_path.rglob("*.py")):
            rel = py_file.relative_to(workspace_path)
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                content = "<unreadable>"
            parts.append(f"### {rel}\n{content}")

        return "\n\n".join(parts)

    @abstractmethod
    def patch_from_prompt(self, prompt_text: str) -> dict:
        """Generate file patches from a natural-language prompt.

        Args:
            prompt_text: Human-readable description of the change to make.

        Returns:
            A dict with key ``"files"``, where each entry has:
                - ``"path"``: file path relative to workspace/
                - ``"content"``: full file content as a string

            Example::

                {
                    "files": [
                        {"path": "app/foo.py", "content": "def foo(): ...\\n"}
                    ]
                }
        """

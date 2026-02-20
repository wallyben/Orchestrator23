from pathlib import Path


class PatchEnforcer:
    """Applies LLM-generated file patches to a workspace directory.

    Usage::

        enforcer = PatchEnforcer(WORKSPACE_DIR)
        enforcer.apply(patch)   # patch = {"files": [{"path": ..., "content": ...}]}
    """

    def __init__(self, workspace_dir):
        self.workspace_dir = Path(workspace_dir)

    def apply(self, patch: dict) -> None:
        """Write each entry in patch["files"] into workspace_dir.

        Creates parent directories as needed.  Overwrites existing files.

        Args:
            patch: dict with key ``"files"``, each entry having:
                - ``"path"``: file path relative to workspace_dir
                - ``"content"``: full file content (string)
        """
        for entry in patch.get("files", []):
            target = self.workspace_dir / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["content"], encoding="utf-8")

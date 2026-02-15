import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    spec_path: str
    workspace_path: str
    logs_path: str
    state_path: str
    max_retries: int
    model_name: str
    api_key: str
    api_timeout: int
    test_timeout: int
    resume: bool

    def validate(self):
        if not os.path.isfile(self.spec_path):
            raise FileNotFoundError(f"Spec file not found: {self.spec_path}")
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Export it before running: export ANTHROPIC_API_KEY=sk-..."
            )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")


BASE_DIR = Path(__file__).parent.resolve()

DEFAULTS = {
    "spec_path": str(BASE_DIR / "spec.md"),
    "workspace_path": str(BASE_DIR / "workspace"),
    "logs_path": str(BASE_DIR / "logs"),
    "state_path": str(BASE_DIR / "state.json"),
    "max_retries": 5,
    "model_name": "claude-sonnet-4-20250514",
    "api_timeout": 120,
    "test_timeout": 60,
}


def load_config(argv=None) -> Config:
    parser = argparse.ArgumentParser(
        description="Orchestrator23 — deterministic local code generation loop"
    )
    parser.add_argument(
        "--spec",
        default=DEFAULTS["spec_path"],
        help="Path to the product spec markdown file",
    )
    parser.add_argument(
        "--workspace",
        default=DEFAULTS["workspace_path"],
        help="Path to the workspace directory for generated files",
    )
    parser.add_argument(
        "--logs",
        default=DEFAULTS["logs_path"],
        help="Path to the logs directory",
    )
    parser.add_argument(
        "--state",
        default=DEFAULTS["state_path"],
        help="Path to state.json",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULTS["max_retries"],
        help="Maximum number of patch retries after initial generation",
    )
    parser.add_argument(
        "--model",
        default=DEFAULTS["model_name"],
        help="Anthropic model name to use",
    )
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=DEFAULTS["api_timeout"],
        help="Timeout in seconds for API calls",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=DEFAULTS["test_timeout"],
        help="Timeout in seconds for test execution",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from existing state.json if present",
    )

    args = parser.parse_args(argv)

    config = Config(
        spec_path=os.path.abspath(args.spec),
        workspace_path=os.path.abspath(args.workspace),
        logs_path=os.path.abspath(args.logs),
        state_path=os.path.abspath(args.state),
        max_retries=args.max_retries,
        model_name=args.model,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        api_timeout=args.api_timeout,
        test_timeout=args.test_timeout,
        resume=args.resume,
    )

    config.validate()
    return config

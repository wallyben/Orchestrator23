import argparse
import os
from dataclasses import dataclass
from pathlib import Path

MAX_RETRIES_HARD_CAP = 50
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 600

VALID_ADAPTERS = ("openai", "anthropic")

ADAPTER_MODEL_DEFAULTS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
}

ADAPTER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


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
    adapter_name: str

    def validate(self):
        if not os.path.isfile(self.spec_path):
            raise FileNotFoundError(f"Spec file not found: {self.spec_path}")
        if self.adapter_name not in VALID_ADAPTERS:
            raise ValueError(
                f"Unknown adapter: {self.adapter_name!r}. "
                f"Valid: {VALID_ADAPTERS}"
            )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.max_retries > MAX_RETRIES_HARD_CAP:
            raise ValueError(
                f"max_retries={self.max_retries} exceeds hard cap of {MAX_RETRIES_HARD_CAP}"
            )
        if not (MIN_TIMEOUT_SECONDS <= self.api_timeout <= MAX_TIMEOUT_SECONDS):
            raise ValueError(
                f"api_timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}, "
                f"got {self.api_timeout}"
            )
        if not (MIN_TIMEOUT_SECONDS <= self.test_timeout <= MAX_TIMEOUT_SECONDS):
            raise ValueError(
                f"test_timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}, "
                f"got {self.test_timeout}"
            )
        ws = os.path.abspath(self.workspace_path)
        if ws in ("/", os.path.expanduser("~")):
            raise ValueError(
                f"workspace_path must not be root or home directory, got: {ws}"
            )


BASE_DIR = Path(__file__).parent.resolve()

DEFAULTS = {
    "spec_path": str(BASE_DIR / "spec.md"),
    "workspace_path": str(BASE_DIR / "workspace"),
    "logs_path": str(BASE_DIR / "logs"),
    "state_path": str(BASE_DIR / "state.json"),
    "max_retries": 5,
    "api_timeout": 120,
    "test_timeout": 60,
    "adapter": "openai",
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
        help=f"Maximum number of patch retries (hard cap: {MAX_RETRIES_HARD_CAP})",
    )
    parser.add_argument(
        "--adapter",
        choices=VALID_ADAPTERS,
        default=DEFAULTS["adapter"],
        help="LLM adapter to use (default: openai)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: adapter-specific)",
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

    adapter_name = args.adapter
    model_name = args.model or ADAPTER_MODEL_DEFAULTS.get(adapter_name, "")
    env_var = ADAPTER_ENV_VARS.get(adapter_name, "")
    api_key = os.environ.get(env_var, "") if env_var else ""

    config = Config(
        spec_path=os.path.abspath(args.spec),
        workspace_path=os.path.abspath(args.workspace),
        logs_path=os.path.abspath(args.logs),
        state_path=os.path.abspath(args.state),
        max_retries=args.max_retries,
        model_name=model_name,
        api_key=api_key,
        api_timeout=args.api_timeout,
        test_timeout=args.test_timeout,
        resume=args.resume,
        adapter_name=adapter_name,
    )

    config.validate()
    return config

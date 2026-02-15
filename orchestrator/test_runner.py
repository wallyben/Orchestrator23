import os
import subprocess
import sys
from dataclasses import dataclass

from config import Config
from logger import Logger


@dataclass
class TestResult:
    passed: bool
    output: str
    return_code: int


class TestRunner:
    def __init__(self, config: Config, logger: Logger):
        self._config = config
        self._logger = logger

    def run_tests(self) -> TestResult:
        workspace = self._config.workspace_path

        if not os.path.isdir(workspace):
            self._logger.error("test_workspace_missing", {"path": workspace})
            return TestResult(
                passed=False,
                output=f"Workspace directory does not exist: {workspace}",
                return_code=1,
            )

        test_files = [
            f
            for f in os.listdir(workspace)
            if f.startswith("test_") and f.endswith(".py")
        ]
        for root, dirs, files in os.walk(workspace):
            for f in files:
                if f.startswith("test_") and f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(root, f), workspace)
                    if rel not in test_files:
                        test_files.append(rel)

        if not test_files:
            self._logger.warn("test_no_test_files", {"workspace": workspace})
            return TestResult(
                passed=False,
                output="No test files found in workspace (expected test_*.py files)",
                return_code=1,
            )

        self._logger.info(
            "test_run_start",
            {"workspace": workspace, "test_files": test_files},
        )

        self._install_requirements(workspace)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-v", "--tb=short", workspace],
                capture_output=True,
                text=True,
                timeout=self._config.test_timeout,
                cwd=workspace,
                env=self._get_env(workspace),
            )
        except subprocess.TimeoutExpired:
            msg = f"Tests timed out after {self._config.test_timeout}s"
            self._logger.error("test_timeout", {"timeout": self._config.test_timeout})
            return TestResult(passed=False, output=msg, return_code=-1)
        except OSError as e:
            msg = f"Failed to execute pytest: {e}"
            self._logger.error("test_exec_error", {"error": str(e)})
            return TestResult(passed=False, output=msg, return_code=-1)

        output = result.stdout
        if result.stderr:
            output += "\n--- STDERR ---\n" + result.stderr

        passed = result.returncode == 0

        self._logger.info(
            "test_run_done",
            {
                "passed": passed,
                "return_code": result.returncode,
                "output_length": len(output),
            },
        )

        return TestResult(
            passed=passed,
            output=output,
            return_code=result.returncode,
        )

    def _install_requirements(self, workspace: str):
        req_path = os.path.join(workspace, "requirements.txt")
        if not os.path.isfile(req_path):
            return

        self._logger.info("test_installing_requirements", {"path": req_path})
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", req_path],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=workspace,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            self._logger.warn("test_requirements_install_failed", {"error": str(e)})

    def _get_env(self, workspace: str) -> dict[str, str]:
        env = os.environ.copy()
        python_path = env.get("PYTHONPATH", "")
        if python_path:
            env["PYTHONPATH"] = workspace + os.pathsep + python_path
        else:
            env["PYTHONPATH"] = workspace
        return env

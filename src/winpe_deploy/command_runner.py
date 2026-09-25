from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


class CommandExecutionError(RuntimeError):
    """Raised when a native Windows tool exits with a non-zero return code."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    return_code: int


class CommandRunner:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def run(
        self, command: Sequence[str], *, secrets: Sequence[str] = (), environment: Mapping[str, str] | None = None
    ) -> CommandResult:
        command_tuple = tuple(str(item) for item in command)
        self._logger.info("Running: %s", self._redact(command_tuple, secrets))
        try:
            completed = subprocess.run(
                command_tuple,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=self._build_environment(environment),
                **self._hidden_window_options(),
            )
        except OSError as error:
            raise CommandExecutionError(f"Could not start {command_tuple[0]}: {error}") from error

        result = CommandResult(command_tuple, completed.stdout, completed.stderr, completed.returncode)
        if result.stdout.strip():
            self._logger.info("Output from %s:\n%s", command_tuple[0], result.stdout.strip())
        if result.stderr.strip():
            self._logger.warning("Error output from %s:\n%s", command_tuple[0], result.stderr.strip())
        if result.return_code != 0:
            message = result.stderr.strip() or result.stdout.strip() or "No diagnostic output was returned."
            raise CommandExecutionError(f"{command_tuple[0]} failed with exit code {result.return_code}: {message}")
        return result

    def run_streaming(
        self,
        command: Sequence[str],
        *,
        output_callback: Callable[[str], None],
        secrets: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a native command and forward console output while it is executing."""
        command_tuple = tuple(str(item) for item in command)
        self._logger.info("Running: %s", self._redact(command_tuple, secrets))
        try:
            process = subprocess.Popen(
                command_tuple,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._build_environment(environment),
                **self._hidden_window_options(),
            )
        except OSError as error:
            raise CommandExecutionError(f"Could not start {command_tuple[0]}: {error}") from error

        output_lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            message = line.strip()
            if not message:
                continue
            output_lines.append(message)
            self._logger.info("Output from %s: %s", command_tuple[0], message)
            output_callback(message)
        return_code = process.wait()
        stdout = "\n".join(output_lines)
        result = CommandResult(command_tuple, stdout, "", return_code)
        if return_code != 0:
            message = stdout or "No diagnostic output was returned."
            raise CommandExecutionError(f"{command_tuple[0]} failed with exit code {return_code}: {message}")
        return result

    @staticmethod
    def _hidden_window_options() -> dict[str, int]:
        """Prevent child console tools from flashing visible windows in WinPE."""
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    @staticmethod
    def _redact(command: Sequence[str], secrets: Sequence[str]) -> str:
        rendered = " ".join(f'"{part}"' if " " in part else part for part in command)
        for secret in secrets:
            if secret:
                rendered = rendered.replace(secret, "********")
        return rendered

    @staticmethod
    def _build_environment(overrides: Mapping[str, str] | None) -> dict[str, str] | None:
        if not overrides:
            return None
        environment = os.environ.copy()
        environment.update({str(key): str(value) for key, value in overrides.items()})
        return environment
from __future__ import annotations

import argparse
from typing import Any

from . import runtime


class OrchestrationError(RuntimeError):
    def __init__(self, message: str, code: str = "orchestration_error") -> None:
        super().__init__(message)
        self.code = code


class CLIUsageError(OrchestrationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "invalid_arguments")


class OrchestrationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if runtime.JSON_MODE:
            raise CLIUsageError(message)
        super().error(message)


class CommandResult:
    def __init__(
        self,
        data: dict[str, Any] | None = None,
        code: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.data = data
        self.code = code
        self.error_code = error_code
        self.error_message = error_message

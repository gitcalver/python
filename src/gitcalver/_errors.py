# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

EXIT_ERROR = 1
EXIT_DIRTY = 2
EXIT_WRONG_BRANCH = 3


class ExitError(Exception):
    def __init__(self, code: int | str = EXIT_ERROR, message: str = "") -> None:
        if isinstance(code, str):
            message = code
            code = EXIT_ERROR
        super().__init__(message)
        self.code = code
        self.message = message

"""Console and file logging configuration for the CLI.

Logging goes to stderr so that stdout stays free for the report the CLI prints,
which keeps ``ora-okf ... > report.txt`` useful.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_CONSOLE_FORMAT = "%(levelname)-7s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure root logging for a CLI run.

    Any handler installed by a previous call is removed first, so repeated calls
    inside one process (the test suite, or an embedding application) do not
    accumulate handlers and print every record several times.

    Args:
        level: One of :data:`LOG_LEVELS`, case-insensitive.
        log_file: Optional file to receive a more detailed log. Its parent
            directories are created if needed.

    Raises:
        ValueError: If ``level`` is not a recognized level name.
    """
    normalized = level.upper()
    if normalized not in LOG_LEVELS:
        raise ValueError(f"Unknown log level '{level}'. Expected one of: {', '.join(LOG_LEVELS)}")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG if log_file is not None else getattr(logging, normalized))

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(getattr(logging, normalized))
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        # The file always gets DEBUG regardless of the console level: the point
        # of asking for a log file is to have the detail available afterwards.
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)

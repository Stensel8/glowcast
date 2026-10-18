"""Shared logging utilities for Glowcast."""

import sys
from datetime import datetime


def timed_print(*args) -> None:
    """Print a line with a timestamp.

    Written in one go, so lines from different threads never interleave.
    """
    message = " ".join(str(arg) for arg in args)
    sys.stdout.write(f"{datetime.now():[%Y-%m-%d %H:%M:%S]} {message}\n")

"""Enables ``python -m loom.cli``."""

from __future__ import annotations

import sys

from loom.cli import main

if __name__ == "__main__":
    sys.exit(main())

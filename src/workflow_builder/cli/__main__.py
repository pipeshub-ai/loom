"""Enables ``python -m workflow_builder.cli``."""

from __future__ import annotations

import sys

from workflow_builder.cli import main

if __name__ == "__main__":
    sys.exit(main())

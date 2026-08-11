"""Shared fixtures for reference workflow tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the examples directory is importable.
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

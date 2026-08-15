"""The standard node library: control, transform, io, and agent nodes.

Importing this module registers every built-in node into the process-global
catalog. It is imported when a :class:`NodeRegistry` is first built, so a
``Runtime`` has the standard library without anybody importing it by hand.

The split between ``control``/``transform`` and ``agent`` is the load-bearing
one: ``control.switch`` is a rule you can write today, ``agent.classify`` is
judgement. Putting them in different categories is what makes choosing between
them deliberate.
"""

from __future__ import annotations

from workflow_builder.nodes.stdlib import agentic, control, io, transform

__all__ = ["agentic", "control", "io", "transform"]

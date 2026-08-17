"""Reading records written by a different version of LOOM.

The journal is the durability substrate: a run recorded by version N has to be
loadable, and replayable, by version N+1 — and by version N-1 for as long as a
rolling deploy has both running. Nothing enforced that. Two entirely ordinary
changes broke it outright:

* **A new enum member.** Statuses and kinds are closed ``StrEnum``\\ s, so the
  first row carrying a value the reading version does not know raised
  ``ValidationError`` from inside ``load_journal``. The run could not be loaded
  at all — not even far enough to report why.
* **A new required field.** Same shape, from the other direction: every
  pre-existing row fails validation, so every in-flight run becomes
  unrecoverable the moment the field ships.

And one that lost data without any error at all: pydantic's default
``extra="ignore"`` drops unknown fields on read, while the stores rewrite the
*whole* record on every write. During a mixed-version window an old pod would
read a record a new pod had written, silently drop the new field, and write it
back without it — indistinguishable from never having been set.

The tools here are the three answers. They are deliberately narrow: tolerance
belongs on the *persisted* models, not on the enums themselves, because
``ExecutionStatus("runnning")`` in application code is a typo that should still
raise rather than quietly become a sentinel.

``core/serde.py`` already reasons this way about the wire envelope — an
unregistered ``__wire__`` tag decodes to its raw data rather than raising,
"because a journal written by a newer deployment must still be readable". This
extends the same principle to the models.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=Enum)

#: The value every tolerant enum falls back to. Chosen to be unmistakable in a
#: dump and impossible to collide with a real member.
UNKNOWN = "__unknown__"


def tolerant_enum(enum_cls: type[E], fallback: E) -> Any:
    """A ``BeforeValidator`` mapping unrecognised values to *fallback*.

    Use on a persisted model's field, never as the enum's own ``_missing_``.
    The difference matters: ``_missing_`` would also swallow a typo written by
    application code, turning a bug into a silent sentinel — which is the class
    of failure this module exists to remove, not to add.

    Logs at warning level, once per value encountered, so an unknown status is
    visible to an operator without flooding a log during a bulk read.
    """
    seen: set[Any] = set()

    def coerce(value: Any) -> Any:
        if value is None or isinstance(value, enum_cls):
            return value
        try:
            return enum_cls(value)
        except ValueError:
            if value not in seen:
                seen.add(value)
                logger.warning(
                    "%s has no member %r; reading it as %s. This record was "
                    "probably written by a newer version of LOOM.",
                    enum_cls.__name__,
                    value,
                    fallback,
                )
            return fallback

    return coerce


def is_unknown(value: Any) -> bool:
    """Whether *value* is a tolerated placeholder rather than a real member.

    Worth branching on before acting: a run whose status could not be read is
    not a run that should be resumed, cancelled, or compacted on a guess.
    """
    return bool(getattr(value, "value", value) == UNKNOWN)

"""P9 Panel ID namespacing.

Identifier format: ``<source>:<kind>:<seq>`` e.g. ``claude:insight:001``.

IDs are *assigned by gotenx*, never authored by the panel models themselves.
This is a deliberate Goodhart defence (P11): the provenance graph that all
deterministic metrics are computed from must not be forgeable by a model.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Panel sources known to the framework. The panel is configurable, but every
# source must be a lowercase token so IDs round-trip through parse/format.
KNOWN_SOURCES = ("claude", "codex", "opencode", "judge")

_TOKEN = r"[a-z][a-z0-9_]*"
_ID_RE = re.compile(rf"^(?P<source>{_TOKEN}):(?P<kind>{_TOKEN}):(?P<seq>\d{{3,}})$")

# Minimum zero-padded width for the sequence component.
SEQ_WIDTH = 3


class InsightId(NamedTuple):
    source: str
    kind: str
    seq: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return make_id(self.source, self.kind, self.seq)


def make_id(source: str, kind: str, seq: int) -> str:
    """Build a canonical namespaced id. Raises ValueError on bad components."""
    if not re.fullmatch(_TOKEN, source):
        raise ValueError(f"invalid source token: {source!r}")
    if not re.fullmatch(_TOKEN, kind):
        raise ValueError(f"invalid kind token: {kind!r}")
    if seq < 0:
        raise ValueError(f"seq must be non-negative: {seq}")
    return f"{source}:{kind}:{seq:0{SEQ_WIDTH}d}"


def parse_id(value: str) -> InsightId:
    """Parse a namespaced id. Raises ValueError if the format is invalid."""
    m = _ID_RE.match(value)
    if not m:
        raise ValueError(f"invalid gotenx id: {value!r}")
    return InsightId(m.group("source"), m.group("kind"), int(m.group("seq")))


def is_valid(value: str) -> bool:
    """True if ``value`` is a well-formed namespaced id."""
    return bool(_ID_RE.match(value))


def source_of(value: str) -> str:
    """Return the source token of a valid id (raises on invalid)."""
    return parse_id(value).source

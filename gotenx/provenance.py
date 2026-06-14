"""P1 Deterministic provenance graph.

Builds the ID graph linking panel insights to Judge plan items via
``source_ids``. Every downstream metric (P10/P16) is a pure function of this
graph, computed without any text matching.

Run-shaped input (the ``panel.json`` / ``judge.json`` artifacts):

    panel = {
      "insights": [
        {"id": "claude:insight:001", "source": "claude",
         "kind": "insight", "content": "..."},
        ...
      ]
    }
    judge = {
      "items": [
        {"id": "judge:plan:001", "source_ids": ["claude:insight:001"],
         "content": "..."},
        ...
      ]
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from . import ids


@dataclass(frozen=True)
class ProvenanceGraph:
    """Immutable view of one run's panel/judge ID linkage."""

    panel_ids: tuple[str, ...]
    panel_sources: tuple[str, ...]
    judge_items: tuple[dict, ...]
    # source_ids that point at a real panel insight
    referenced_panel_ids: frozenset[str]
    # source_ids the Judge cited that do NOT exist in the panel (P11 hardening:
    # dangling citations are a faithfulness red flag, surfaced not silently
    # dropped).
    dangling_source_ids: frozenset[str]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def panel_id_set(self) -> frozenset[str]:
        return frozenset(self.panel_ids)

    def judge_source_ids(self, item: dict) -> list[str]:
        return list(item.get("source_ids", []))


def build_graph(panel: dict, judge: dict) -> ProvenanceGraph:
    """Construct the provenance graph from panel + judge artifacts.

    Validates that every panel insight and every cited ``source_id`` is a
    well-formed namespaced id (P9). Malformed ids raise ValueError; that is a
    structural error, not a metric to be silently degraded.
    """
    panel_insights = panel.get("insights", [])
    panel_ids: list[str] = []
    panel_sources: list[str] = []
    warnings: list[str] = []

    for ins in panel_insights:
        iid = ins["id"]
        parsed = ids.parse_id(iid)  # raises on malformed id
        declared = ins.get("source")
        if declared is not None and declared != parsed.source:
            warnings.append(
                f"insight {iid} declared source {declared!r} != id source {parsed.source!r}"
            )
        panel_ids.append(iid)
        panel_sources.append(parsed.source)

    panel_id_set = set(panel_ids)
    if len(panel_id_set) != len(panel_ids):
        raise ValueError("duplicate panel insight ids in run")

    judge_items = judge.get("items", [])
    referenced: set[str] = set()
    dangling: set[str] = set()
    for item in judge_items:
        for sid in item.get("source_ids", []):
            ids.parse_id(sid)  # validate shape
            if sid in panel_id_set:
                referenced.add(sid)
            else:
                dangling.add(sid)

    if dangling:
        warnings.append(f"{len(dangling)} dangling source_id(s) not present in panel")

    return ProvenanceGraph(
        panel_ids=tuple(panel_ids),
        panel_sources=tuple(panel_sources),
        judge_items=tuple(judge_items),
        referenced_panel_ids=frozenset(referenced),
        dangling_source_ids=frozenset(dangling),
        warnings=tuple(warnings),
    )


def distinct_sources(panel_ids: Iterable[str]) -> set[str]:
    """Distinct panel source tokens for a set of ids."""
    return {ids.source_of(i) for i in panel_ids}

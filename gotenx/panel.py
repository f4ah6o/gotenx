"""Panel runner: collect insights from each analyst source.

Each source is asked for a JSON array of ``{kind, content}`` objects. gotenx
then *assigns* the namespaced id ``<source>:<kind>:<seq>`` (P9). The model never
supplies its own id, so the provenance graph cannot be forged (P11).
"""

from __future__ import annotations

from collections import defaultdict

from . import ids
from .llm import Transport, extract_json_array

PANEL_PROMPT = (
    "You are an analyst on a deliberation panel reviewing the following task.\n"
    "Return ONLY a JSON array. Each element is an object with exactly two keys:\n"
    '  "kind"    - one short lowercase token, e.g. "insight" or "blindspot"\n'
    '  "content" - one or two sentences capturing a single distinct point\n'
    "Do not include ids, prose, or markdown fences. Task:\n\n{task}"
)


def _assign_ids(source: str, raw_insights: list) -> list[dict]:
    """Assign deterministic ids per (source, kind), sequencing within kind."""
    seqs: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    for index, item in enumerate(raw_insights):
        if not isinstance(item, dict):
            raise ValueError(f"panel item {index} must be an object")
        kind = str(item.get("kind", "insight")).strip().lower() or "insight"
        # sanitise kind to a valid token
        kind = "".join(c if (c.isalnum() or c == "_") else "_" for c in kind)
        if not kind or not kind[0].isalpha():
            kind = "insight"
        seqs[kind] += 1
        out.append(
            {
                "id": ids.make_id(source, kind, seqs[kind]),
                "source": source,
                "kind": kind,
                "content": str(item.get("content", "")).strip(),
            }
        )
    return out


def run_panel(sources: list[str], task: str, transport: Transport) -> dict:
    """Run all panel sources, returning a ``panel`` artifact + warnings."""
    insights: list[dict] = []
    warnings: list[str] = []
    contributed: list[str] = []
    usage: dict[str, dict | None] = {}
    prompt = PANEL_PROMPT.format(task=task)
    for source in sources:
        try:
            result = transport.invoke_result(source, prompt, slot=f"panel/{source}")
        except Exception as exc:  # noqa: BLE001 - one source failing must not kill the run
            warnings.append(f"panel source {source!r} produced no usable output: {exc}")
            continue
        usage[source] = result.usage
        try:
            parsed = extract_json_array(result.raw)
        except Exception as exc:  # noqa: BLE001 - one source failing must not kill the run
            warnings.append(f"panel source {source!r} produced no usable output: {exc}")
            continue
        assigned = _assign_ids(source, parsed)
        if assigned:
            contributed.append(source)
        insights.extend(assigned)
    return {
        "insights": insights,
        "panels": contributed,
        "warnings": warnings,
        "usage": usage,
    }

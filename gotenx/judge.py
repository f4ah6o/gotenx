"""Judge runner: synthesise panel insights into a plan with provenance.

The Judge receives every panel insight WITH its gotenx-assigned id and must
cite the ids it acted on via ``source_ids`` (P1/P7). gotenx assigns the judge
item ids (``judge:plan:NNN``); the Judge supplies only ``content`` +
``source_ids``. Citations to non-existent ids are kept as warnings rather than
silently dropped, so the Eval-layer ``provenance_faithful`` check (P11) can see
them.
"""

from __future__ import annotations

import json

from . import ids
from .llm import Transport, extract_json_array

JUDGE_PROMPT = (
    "You are the Judge. Synthesise the panel insights below into a concrete "
    "plan.\n"
    "Return ONLY a JSON array. Each element is an object with exactly two keys:\n"
    '  "content"    - one plan item (a concrete action or conclusion)\n'
    '  "source_ids" - a list of the panel insight ids this item acted on;\n'
    "                 cite only ids that genuinely support the item.\n"
    "Do not invent ids. Do not include markdown fences. Panel insights:\n\n"
    "{insights}"
)


def _format_insights(panel: dict) -> str:
    lines = []
    for ins in panel.get("insights", []):
        lines.append(f'{ins["id"]}  [{ins["source"]}/{ins["kind"]}]  {ins["content"]}')
    return "\n".join(lines)


def run_judge(panel: dict, judge_source: str, transport: Transport) -> dict:
    """Run the Judge over a panel artifact, returning a ``judge`` artifact."""
    prompt = JUDGE_PROMPT.format(insights=_format_insights(panel))
    warnings: list[str] = []
    try:
        result = transport.invoke_result(judge_source, prompt, slot="judge/judge")
    except Exception as exc:  # noqa: BLE001
        return {
            "items": [],
            "warnings": [f"judge produced no usable output: {exc}"],
            "usage": None,
        }
    usage = result.usage
    try:
        parsed = extract_json_array(result.raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "items": [],
            "warnings": [f"judge produced no usable output: {exc}"],
            "usage": usage,
        }

    items: list[dict] = []
    for i, raw_item in enumerate(parsed, start=1):
        if not isinstance(raw_item, dict):
            return {"items": [], "warnings": [f"judge item {i} must be an object"], "usage": usage}
        source_ids = raw_item.get("source_ids", []) or []
        if not isinstance(source_ids, list):
            return {"items": [], "warnings": [f"judge item {i} source_ids must be a list"], "usage": usage}
        clean_ids = []
        for sid in source_ids:
            if ids.is_valid(str(sid)):
                clean_ids.append(str(sid))
            else:
                warnings.append(f"judge item {i} cited malformed id {sid!r} (dropped)")
        items.append(
            {
                "id": ids.make_id("judge", "plan", i),
                "source_ids": clean_ids,
                "content": str(raw_item.get("content", "")).strip(),
            }
        )
    return {"items": items, "warnings": warnings, "usage": usage}


def main() -> None:  # pragma: no cover - convenience for manual use
    import sys

    panel = json.load(sys.stdin)
    print(json.dumps(run_judge(panel, "claude", Transport(mode="real")), indent=2))

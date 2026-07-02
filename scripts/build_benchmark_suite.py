#!/usr/bin/env python3
"""Build the fixed 60-case benchmark manifest from pinned public PRs."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SYNTHETIC = [
    "Design a backwards-compatible cache for a read-heavy API and specify invalidation, observability, and tests.",
    "Plan a parser refactor that improves diagnostics without changing accepted syntax or public APIs.",
    "Review a proposed concurrency fix for races, cancellation, resource cleanup, and deterministic tests.",
    "Plan a dependency upgrade that changes transitive platform bindings while preserving supported targets.",
    "Review a numeric boundary fix for signedness, adjacent representable values, overflow, and cross-target behavior.",
]


def fetch(url: str, accept: str = "application/vnd.github+json") -> str:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "gotenx-benchmark-builder"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def public_kind(language_index: int, item_index: int) -> str:
    plan_count = 3 if language_index < 3 else 2
    return "plan" if item_index < plan_count else "review"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default=str(ROOT / "benchmarks/v1/sources.json"))
    parser.add_argument("--output", default=".gotenx/benchmarks/v1/manifest.json")
    parser.add_argument("--offline", action="store_true", help="omit remote PR body/diff context")
    args = parser.parse_args()
    sources = json.loads(Path(args.sources).read_text())
    languages = ["python", "typescript", "go", "rust", "swift", "moonbit"]
    cases = []
    grouped = {language: [s for s in sources if s["language"] == language] for language in languages}
    for language_index, language in enumerate(languages):
        for item_index, source in enumerate(grouped[language]):
            kind = public_kind(language_index, item_index)
            url = f"https://github.com/{source['repo']}/pull/{source['number']}"
            body = ""
            if not args.offline:
                if kind == "review":
                    body = fetch(url + ".diff", "application/vnd.github.v3.diff")[:16000]
                else:
                    api = f"https://api.github.com/repos/{source['repo']}/pulls/{source['number']}"
                    body = json.loads(fetch(api)).get("body") or ""
            cases.append({
                "id": f"public-{language}-{item_index + 1:02d}", "kind": kind,
                "origin": "public_pr", "language": language,
                "task": ("Plan the change described by" if kind == "plan" else "Review the patch from") + f" PR #{source['number']}: {source['title']}",
                "context": body,
                "source": {"url": url, "commit": source["commit"], "license": source["license"]},
            })
        synthetic_plan_count = 2 if language_index < 3 else 3
        for item_index, task in enumerate(SYNTHETIC):
            kind = "plan" if item_index < synthetic_plan_count else "review"
            cases.append({
                "id": f"synthetic-{language}-{item_index + 1:02d}", "kind": kind,
                "origin": "synthetic", "language": language,
                "task": f"In a {language} codebase: {task}",
                "context": "Treat compatibility, failure modes, tests, rollout, and measurable acceptance criteria as required.",
            })
    manifest = {"id": "gotenx-v1", "schema_version": 1, "cases": cases}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

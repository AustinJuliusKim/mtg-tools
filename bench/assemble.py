"""Collect bench/out/*.json lines into a table, after asserting the oracles agree.

A speed number from a lane that parsed a different reality is worthless, so
cross-lane oracle equality is a hard failure, not a footnote.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def main() -> int:
    by_dataset = defaultdict(list)
    for name in sorted(os.listdir(OUT)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(OUT, name), encoding="utf-8") as handle:
            result = json.load(handle)
        by_dataset[result["dataset"]].append(result)

    failures = []
    for dataset, results in by_dataset.items():
        oracles = {
            r["lane"]: r["oracle"] for r in results
            if r["oracle"] and r["oracle"]["pre"] is not None
        }
        distinct = {json.dumps(o, sort_keys=True) for o in oracles.values()}
        if len(distinct) > 1:
            failures.append((dataset, oracles))

    if failures:
        for dataset, oracles in failures:
            print(f"ORACLE MISMATCH on {dataset}:", file=sys.stderr)
            for lane, oracle in sorted(oracles.items()):
                print(f"  {lane}: {json.dumps(oracle, sort_keys=True)}", file=sys.stderr)
        return 1

    for dataset, results in by_dataset.items():
        sample = next(r for r in results if r["oracle"] and r["oracle"]["pre"])
        pre = sample["oracle"]["pre"]
        print(f"\n## {dataset} — {pre['rows']:,} rows, {pre['quantity']:,} cards, "
              f"${pre['valueCents'] / 100:,.2f} (all lanes agree)\n")
        stage_keys = ["read", "fetch", "parse", "merge", "summarize"]
        print("| lane | " + " | ".join(stage_keys) + " | total ms | rows/s (parse) |")
        print("|---|" + "---|" * (len(stage_keys) + 2))
        for r in sorted(results, key=lambda r: r["totalMedianMs"]):
            stages = dict(r["stagesMedianMs"])
            if "fetchMs" in r:
                stages["fetch"] = r["fetchMs"]
            cells = [f"{stages[k]:.1f}" if k in stages else "—" for k in stage_keys]
            parse_ms = stages.get("parse", 0)
            rps = f"{pre['rows'] / (parse_ms / 1000):,.0f}" if parse_ms else "—"
            print(f"| {r['lane']} | " + " | ".join(cells)
                  + f" | {r['totalMedianMs']:.1f} | {rps} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())

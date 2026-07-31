"""Python lanes of the CSV benchmark.

Lanes:
  app   — the repo's real code: binders.io parse -> aggregate.merge -> summarize.
          This is what the server actually does on an import.
  fast  — the same parsing semantics with integer cents and plain dicts instead
          of Decimal and dataclasses. Separates "Python is slow" from "Decimal
          and dataclass construction are slow".
  stage — python-only extra: webapp.importer.stage_import into :memory: SQLite,
          the full production staging path (parse + validate + 3x json.dumps
          per row + executemany). No JS equivalent; reported for context.

Protocol (all lanes, all runners): 2 warmup runs, then N timed runs in-process;
stages timed separately; the oracle printed so run_all can assert every lane
parsed the same reality before comparing speed.

Oracle: rows, quantity, valueCents before merge; same triple after merge.
Prices in the datasets are two-decimal, so integer cents are exact.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import resource
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from binders import aggregate  # noqa: E402
from binders import io as bio  # noqa: E402
from binders import sealed as bsealed  # noqa: E402
from binders.model import Collection  # noqa: E402


def read_text(paths):
    texts = []
    for path in paths:
        with open(path, encoding="utf-8-sig") as handle:
            texts.append(handle.read())
    return texts


# --- app lane: the real pipeline --------------------------------------------


def app_parse(texts, names):
    # Mirrors binders.io.load exactly, minus the open() — reading is its own
    # timed stage so parse cost is comparable across lanes.
    cards = []
    for text, name in zip(texts, names):
        reader = csv.DictReader(io.StringIO(text))
        if "Title" not in bio.canonical_header(reader.fieldnames or []).values():
            raise SystemExit(f"unrecognized header in {name}")
        source = os.path.splitext(os.path.basename(name))[0]
        cards.extend(bio.parse_row(row, source=source) for row in reader if any(row.values()))
    return Collection(cards)


def oracle_of(collection) -> dict:
    return {
        "rows": len(collection),
        "quantity": collection.total_quantity,
        "valueCents": int(collection.total_value * 100),
    }


# --- fast lane: same semantics, cents + dicts --------------------------------


def money_cents(raw) -> int:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return 0
    try:
        if "." in text:
            whole, frac = text.split(".", 1)
            frac = (frac + "00")[:2]
        else:
            whole, frac = text, "00"
        sign = -1 if whole.startswith("-") else 1
        return sign * (abs(int(whole or "0")) * 100 + int(frac))
    except ValueError:
        return 0


FINISH_WORDS = {"normal", "nonfoil", "non-foil", "foil", "etched", "etched foil"}
TRUE_WORDS = {"true", "1", "yes", "y", "t"}


def fast_parse(texts, names):
    rows = []
    for text, name in zip(texts, names):
        reader = csv.DictReader(io.StringIO(text))
        header = bio.canonical_header(reader.fieldnames or [])
        for raw in reader:
            if not any(raw.values()):
                continue
            row = {header.get(k, k): v for k, v in raw.items()}
            foil_text = (row.get("Foil") or "").strip().lower()
            if foil_text in FINISH_WORDS:
                finish = "etched" if foil_text.startswith("etched") else (
                    "normal" if foil_text in ("normal", "nonfoil", "non-foil") else "foil")
                foil = finish != "normal"
            else:
                foil, finish = foil_text in TRUE_WORDS, ""
            q = (row.get("Quantity") or "1").strip()
            quantity = int(q) if q.lstrip("-").isdigit() else 1
            scryfall = (row.get("Scryfall ID") or "").strip()
            identity = (scryfall, finish or ("foil" if foil else "normal"))
            rows.append({
                "title": (row.get("Title") or "").strip(),
                "identity": identity,
                "quantity": quantity,
                "cents": money_cents(row.get("Purchase price")),
                "rarity": (row.get("Rarity") or "").strip().lower(),
                "set": (row.get("Edition") or "").strip(),
                "added": (row.get("Added") or "").strip(),
                "foil": foil,
            })
    return rows


def fast_merge(rows):
    merged = {}
    for row in rows:
        cur = merged.get(row["identity"])
        if cur is None:
            merged[row["identity"]] = dict(row)
        else:
            cur["quantity"] += row["quantity"]
            if row["added"] and row["added"] > cur["added"]:
                cur["cents"] = row["cents"]
                cur["added"] = row["added"]
    return list(merged.values())


def fast_oracle(rows) -> dict:
    return {
        "rows": len(rows),
        "quantity": sum(r["quantity"] for r in rows),
        "valueCents": sum(r["cents"] * r["quantity"] for r in rows),
    }


def fast_summarize(rows):
    by_set = {}
    for r in rows:
        q, v = by_set.get(r["set"], (0, 0))
        by_set[r["set"]] = (q + r["quantity"], v + r["cents"] * r["quantity"])
    prices = sorted(r["cents"] for r in rows)
    return {
        "sets": len(by_set),
        "median": prices[len(prices) // 2] if prices else 0,
        "foilQuantity": sum(r["quantity"] for r in rows if r["foil"]),
    }


# --- sealed lanes ------------------------------------------------------------


def sealed_app(texts, names):
    holdings = []
    for text in texts:
        import tempfile
        # load_sealed reads a path; a NamedTemporaryFile keeps the real code path.
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(text)
            tmp = handle.name
        try:
            holdings.extend(bsealed.load_sealed(tmp))
        finally:
            os.unlink(tmp)
    return holdings


def sealed_oracle(holdings) -> dict:
    total = sum((h.price or 0) * h.quantity for h in holdings)
    return {
        "rows": len(holdings),
        "quantity": sum(h.quantity for h in holdings),
        "valueCents": int(total * 100),
    }


# --- stage lane (python-only context) ----------------------------------------


def stage_once(texts, names):
    from webapp import db as wdb
    from webapp import importer
    conn = wdb.connect(":memory:")
    wdb.init_db(conn)
    total = 0
    for text, name in zip(texts, names):
        import_id, _ = importer.stage_import(conn, name, text.encode("utf-8"))
        total += 1
    conn.close()
    return total


# --- harness -----------------------------------------------------------------


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


def run(lane, paths, runs, warmup):
    names = [os.path.basename(p) for p in paths]
    samples = []
    oracle = None
    for i in range(warmup + runs):
        stages = {}
        stages["read"], texts = timed(lambda: read_text(paths))
        if lane == "app":
            stages["parse"], coll = timed(lambda: app_parse(texts, names))
            stages["merge"], merged = timed(lambda: aggregate.merge(coll))
            stages["summarize"], _ = timed(lambda: aggregate.summarize(merged))
            oracle = {"pre": oracle_of(coll), "post": oracle_of(merged)}
        elif lane == "fast":
            stages["parse"], rows = timed(lambda: fast_parse(texts, names))
            stages["merge"], merged = timed(lambda: fast_merge(rows))
            stages["summarize"], _ = timed(lambda: fast_summarize(merged))
            oracle = {"pre": fast_oracle(rows), "post": fast_oracle(merged)}
        elif lane == "sealed":
            stages["parse"], holdings = timed(lambda: sealed_app(texts, names))
            oracle = {"pre": sealed_oracle(holdings), "post": sealed_oracle(holdings)}
        elif lane == "stage":
            stages["parse"], _ = timed(lambda: stage_once(texts, names))
            oracle = {"pre": None, "post": None}
        else:
            raise SystemExit(f"unknown lane {lane!r}")
        if i >= warmup:
            samples.append(stages)
    medians = {
        k: round(statistics.median(s[k] for s in samples), 2)
        for k in samples[0]
    }
    return {
        "lane": f"python-{lane}",
        "python": sys.version.split()[0],
        "files": names,
        "stagesMedianMs": medians,
        "totalMedianMs": round(sum(medians.values()), 2),
        "runs": runs,
        "oracle": oracle,
        "maxRssMb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True, choices=["app", "fast", "sealed", "stage"])
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    print(json.dumps(run(args.lane, args.files, args.runs, args.warmup)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

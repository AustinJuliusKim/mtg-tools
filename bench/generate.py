"""Deterministic synthetic datasets in the exact dialects the app ingests.

Legacy ManaBox header (what the real Desktop exports use), word-dialect Foil,
~25% of titles containing a comma (so quoted-field handling is actually
exercised — a split(',') parser must fail, not win), ~30% duplicate identities
(so merge has real work), 2-decimal prices (so integer-cents JS and Decimal
Python agree exactly).

Seeded: the same files fall out every run, so lanes always see the same bytes.
"""

from __future__ import annotations

import csv
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

LEGACY_HEADER = [
    "Name", "Set code", "Set name", "Collector number", "Foil", "Rarity",
    "Quantity", "ManaBox ID", "Scryfall ID", "Purchase price", "Misprint",
    "Altered", "Condition", "Language", "Purchase price currency", "Added",
]

SEALED_HEADER = [
    "Name", "Set", "Quantity", "Condition", "Price", "Price date", "Source",
    "Cost basis", "Notes",
]

ADJ = ["Ancient", "Gilded", "Whispering", "Sunken", "Feral", "Radiant",
       "Mournful", "Iron", "Verdant", "Hollow", "Storm-Touched", "Grim"]
NOUN = ["Sphinx", "Elves", "Colossus", "Tutor", "Cavern", "Regent", "Sanctum",
        "Marauder", "Oracle", "Aegis", "Reclaimer", "Wurm"]
SUFFIX = ["of the Deep", "of Ruin", "the Unbroken", "of Dawn's Gate",
          "the Everliving", "of the Ninth Sphere"]
SETS = [(f"SB{i:02d}", f"Synthetic Block {i}") for i in range(1, 41)]
RARITIES = ["common", "uncommon", "rare", "mythic"]
FOILS = ["normal"] * 16 + ["foil"] * 3 + ["etched"]


def _title(rng: random.Random) -> str:
    base = f"{rng.choice(ADJ)} {rng.choice(NOUN)}"
    roll = rng.random()
    if roll < 0.25:
        # The comma is the point: it forces real quoted-field parsing.
        return f"{base}, {rng.choice(SUFFIX)}"
    if roll < 0.4:
        return f"{base} {rng.choice(SUFFIX)}"
    return base


def _price(rng: random.Random) -> str:
    if rng.random() < 0.10:
        return ""  # unpriced — money() maps this to 0
    # Log-ish spread from bulk to chase card, always two decimals.
    cents = int(10 ** rng.uniform(0.7, 4.4))
    return f"{cents // 100}.{cents % 100:02d}"


def _added(rng: random.Random) -> str:
    day = rng.randint(1, 28)
    return f"2026-{rng.randint(1, 7):02d}-{day:02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999):03d}Z"


def singles(n: int, path: str, seed: int = 20260730) -> None:
    rng = random.Random(seed)
    pool = []  # earlier cards, re-emitted for the duplicate fraction
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(LEGACY_HEADER)
        for i in range(n):
            if pool and rng.random() < 0.30:
                row = list(rng.choice(pool))
                row[6] = str(rng.randint(1, 4))  # same card, new quantity
                writer.writerow(row)
                continue
            code, set_name = rng.choice(SETS)
            row = [
                _title(rng), code, set_name, str(rng.randint(1, 400)),
                rng.choice(FOILS), rng.choice(RARITIES), str(rng.randint(1, 4)),
                str(rng.randint(10_000, 99_999)),
                f"{rng.getrandbits(128):032x}", _price(rng), "false", "false",
                "near_mint", "en", "USD", _added(rng),
            ]
            pool.append(row)
            writer.writerow(row)


def sealed(n: int, path: str, seed: int = 20260730) -> None:
    rng = random.Random(seed)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SEALED_HEADER)
        for i in range(n):
            code, set_name = rng.choice(SETS)
            price = _price(rng)
            writer.writerow([
                f"{_title(rng)} Commander Deck", code, str(rng.randint(1, 3)),
                rng.choice(["sealed", "sealed", "sealed", "opened"]),
                price,
                f"2026-{rng.randint(1, 7):02d}-{rng.randint(1, 28):02d}" if price else "",
                rng.choice(["tcgplayer", "ebay-sold", ""]),
                _price(rng) if rng.random() < 0.5 else "",
                "",
            ])


def main() -> int:
    os.makedirs(DATA, exist_ok=True)
    jobs = [
        ("synth-20k.csv", lambda p: singles(20_000, p)),
        ("synth-100k.csv", lambda p: singles(100_000, p, seed=20260731)),
        ("sealed-20k.csv", lambda p: sealed(20_000, p)),
    ]
    for name, build in jobs:
        path = os.path.join(DATA, name)
        build(path)
        with open(path, encoding="utf-8") as handle:
            rows = sum(1 for _ in handle) - 1
        print(f"{name}: {rows} rows, {os.path.getsize(path):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

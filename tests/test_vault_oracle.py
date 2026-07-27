"""Regression tests against the real exports and the hand-built vault tables.

The CK Buylist Estimates and Multi-Copy Flags tables in
`ObsidianVault/30-projects/Paternity Leave Project Plan.md` were computed by
hand from the `.bak` exports. They are an independent oracle for the money math
here: if `price_tiers()` reproduces that table to the dollar, the tiering,
quantity handling and Decimal arithmetic are all correct.

Everything skips when the exports are not on disk, so the suite still runs on a
machine that only has the repo.
"""

from __future__ import annotations

import os
import unittest
from decimal import Decimal

from binders import diff, load, load_many, merge, multi_copies, price_tiers, summarize
from tests.support import (
    BINDERS,
    BINDERS2,
    BINDERS2_BAK,
    BINDERS_BAK,
    require_exports as _require,
)


class TestVaultTable(unittest.TestCase):
    """The `.bak` exports are what the vault's published table was built from."""

    def setUp(self):
        _require(BINDERS_BAK, BINDERS2_BAK)
        self.cards = load_many(BINDERS_BAK, BINDERS2_BAK)

    def test_scanned_total_matches_the_vault(self):
        # Vault: "$9,831 · 752 cards"
        self.assertEqual(self.cards.total_quantity, 752)
        self.assertEqual(round(self.cards.total_value), 9831)

    def test_per_binder_totals_match_the_vault(self):
        # Vault: Binder 1+2 = $2,833; Binder 3 first half = $6,998.
        self.assertEqual(round(load(BINDERS_BAK).total_value), 2833)
        self.assertEqual(round(load(BINDERS2_BAK).total_value), 6998)

    def test_tier_card_counts_and_market_values_match_the_vault(self):
        """The exact half of the oracle: counts and market value.

        These come straight out of the data with no rate applied, so they must
        match the published table to the dollar.
        """
        rows = price_tiers(self.cards)

        # | $20+ (prime)    | 129 cards | $7,101 |
        self.assertEqual(rows["prime"].quantity, 129)
        self.assertEqual(rows["prime"].market_value, Decimal("7100.85"))

        # | $5–$19.99 (mid) | 199 cards | $2,065 |
        self.assertEqual(rows["mid"].quantity, 199)
        self.assertEqual(rows["mid"].market_value, Decimal("2064.53"))

        # | Under $5 (bulk) | 424 cards | $666   |
        self.assertEqual(rows["bulk"].quantity, 424)
        self.assertEqual(rows["bulk"].market_value, Decimal("666.00"))

    def test_tier_estimates_match_the_vault_within_rounding(self):
        """The approximate half: cash and credit estimates.

        The vault's figures are hand-rounded ("~$4,260"), and at least one
        differs from banker-free half-up rounding by a dollar — $7,100.85 x
        0.60 is $4,260.51, published as $4,260. A dollar of slack on a
        tens-of-thousands estimate is the right tolerance; the rates and the
        inputs are pinned exactly elsewhere.
        """
        rows = price_tiers(self.cards)
        for tier, cash, credit in [
            ("prime", 4260, 5326),
            ("mid", 970, 1280),
            ("bulk", 133, 167),
        ]:
            with self.subTest(tier=tier):
                self.assertAlmostEqual(float(rows[tier].cash), cash, delta=1)
                self.assertAlmostEqual(float(rows[tier].credit), credit, delta=1)

    def test_tier_totals_are_internally_consistent(self):
        """Totals must equal the sum of the bands exactly — that is an invariant."""
        rows = price_tiers(self.cards)
        self.assertEqual(
            sum(r.market_value for r in rows.values()), self.cards.total_value
        )
        self.assertEqual(sum(r.quantity for r in rows.values()), self.cards.total_quantity)

    def test_tier_totals_match_the_vault_within_rounding(self):
        """Vault totals are the sum of three already-rounded rows.

        4260 + 970 + 133 = 5363, where the exact figures total $5,364.04. Three
        compounded hand-roundings, so the tolerance here is looser than the
        per-row one.
        """
        rows = price_tiers(self.cards)
        self.assertAlmostEqual(float(sum(r.cash for r in rows.values())), 5363, delta=2)
        self.assertAlmostEqual(float(sum(r.credit for r in rows.values())), 6773, delta=2)

    def test_multi_copy_flags_match_the_vault(self):
        stacks = {c.display_name: c for c in multi_copies(self.cards, min_qty=4)}
        expected = {
            "Mox Amber": (13, 75, 977),
            "Mystical Tutor": (7, 16, 112),
            "Ashnod's Altar": (6, 15, 91),
            "Worldly Tutor": (5, 27, 134),
            "Holistic Wisdom (foil)": (4, 47, 188),
            "Doubling Season (foil)": (4, 36, 144),
            "Exploration": (4, 33, 133),
            "Griselbrand (foil)": (4, 18, 71),
        }
        for name, (qty, each, total) in expected.items():
            with self.subTest(card=name):
                self.assertIn(name, stacks)
                card = stacks[name]
                self.assertEqual(card.quantity, qty)
                self.assertEqual(round(card.market_price), each)
                self.assertEqual(round(card.total_value), total)


class TestLegacyDialect(unittest.TestCase):
    """The .bak exports use the older ManaBox header, and must still parse."""

    def setUp(self):
        _require(BINDERS_BAK)
        self.cards = load(BINDERS_BAK)

    def test_name_column_populates_title(self):
        self.assertTrue(all(c.title for c in self.cards))
        self.assertEqual(self.cards[0].title, "Tangleweave Armor")

    def test_set_code_column_populates_edition(self):
        self.assertEqual(self.cards[0].edition, "ONC")
        self.assertEqual(self.cards[0].set_name, "Phyrexia: All Will Be One Commander")

    def test_word_foil_dialect_is_read(self):
        self.assertFalse(self.cards[0].foil)
        self.assertEqual(self.cards[0].finish, "normal")
        self.assertTrue(any(c.foil for c in self.cards))

    def test_aliased_columns_do_not_leak_into_extra(self):
        self.assertEqual(self.cards[0].extra, {})

    def test_legacy_and_current_exports_merge_on_the_same_identity(self):
        _require(BINDERS)
        current = load(BINDERS)
        shared = {c.identity for c in self.cards} & {c.identity for c in current}
        self.assertGreater(len(shared), 200)

    def test_a_header_with_no_name_column_is_rejected(self):
        import tempfile

        from binders.io import UnknownSchema

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Card,Qty,Price\nSol Ring,1,2.00\n")
            with self.assertRaises(UnknownSchema):
                load(path)


class TestCurrentExports(unittest.TestCase):
    """Today's files, after the manual prune."""

    def setUp(self):
        _require(BINDERS, BINDERS2)
        self.raw = load_many(BINDERS, BINDERS2)
        self.merged = merge(self.raw)

    def test_raw_totals(self):
        self.assertEqual(len(self.raw), 415)
        self.assertEqual(self.raw.total_quantity, 543)
        self.assertEqual(self.raw.total_value, Decimal("9737.83"))

    def test_per_file_totals(self):
        self.assertEqual(load(BINDERS).total_value, Decimal("2748.08"))
        self.assertEqual(load(BINDERS2).total_value, Decimal("6989.75"))

    def test_merge_collapses_the_five_shared_cards(self):
        self.assertEqual(len(self.merged), 410)
        self.assertEqual(self.merged.total_quantity, 543)
        shared = [c for c in self.merged if len(c.sources) > 1]
        self.assertEqual(len(shared), 5)

    def test_merged_value_uses_the_later_scan(self):
        # The five shared cards were rescanned a week later at slightly
        # different prices, so the merged total is not the raw total.
        self.assertEqual(self.merged.total_value, Decimal("9735.73"))

    def test_a_shared_card_can_cross_a_tier_boundary(self):
        card = next(c for c in self.merged if c.title == "Black Market Connections")
        self.assertEqual(card.market_price, Decimal("19.7"))  # was 20.23 in June
        self.assertEqual(card.tier, "mid")

    def test_no_rows_are_lost_or_duplicated_by_merging(self):
        self.assertEqual(self.merged.total_quantity, self.raw.total_quantity)
        self.assertEqual(len({c.identity for c in self.merged}), len(self.merged))

    def test_language_flag_is_the_only_validation_issue(self):
        from binders import validate

        issues = validate(self.raw)
        self.assertEqual([i.code for i in issues], ["language"])


class TestPruneDiff(unittest.TestCase):
    """What the manual `.bak` -> current prune actually removed."""

    def setUp(self):
        _require(BINDERS, BINDERS2, BINDERS_BAK, BINDERS2_BAK)
        self.result = diff(
            load_many(BINDERS_BAK, BINDERS2_BAK), load_many(BINDERS, BINDERS2)
        )

    def test_the_prune_removed_bulk(self):
        removed = self.result.removed
        self.assertGreater(len(removed), 100)
        bulk = [c for c in removed if c.tier == "bulk"]
        self.assertGreater(len(bulk) / len(removed), 0.9)

    def test_net_quantity_matches_the_two_totals(self):
        before = load_many(BINDERS_BAK, BINDERS2_BAK)
        after = load_many(BINDERS, BINDERS2)
        self.assertEqual(
            self.result.quantity_delta,
            merge(after).total_quantity - merge(before).total_quantity,
        )

    def test_value_delta_reconciles(self):
        before = merge(load_many(BINDERS_BAK, BINDERS2_BAK))
        after = merge(load_many(BINDERS, BINDERS2))
        self.assertEqual(self.result.value_delta, after.total_value - before.total_value)


if __name__ == "__main__":
    unittest.main()

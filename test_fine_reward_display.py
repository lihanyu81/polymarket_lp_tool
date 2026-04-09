"""Fine-tick reward interval display: book span or tick-snapped theory."""

from __future__ import annotations

import unittest

from passive_liquidity.simple_price_policy import (
    fine_reward_display_lo_hi,
    fine_tick_display_decimals,
)


def _b(p: float, s: float = 1.0) -> dict:
    return {"price": p, "size": s}


class TestFineRewardDisplay(unittest.TestCase):
    def test_theory_snap_inward_001(self) -> None:
        lo, hi, book = fine_reward_display_lo_hi(
            0.9565,
            0.035,
            0.001,
            [],
            [],
        )
        self.assertFalse(book)
        self.assertAlmostEqual(lo, 0.922, places=6)
        self.assertAlmostEqual(hi, 0.991, places=6)

    def test_book_span_uses_min_max_symmetric_legacy(self) -> None:
        bids = [_b(0.923), _b(0.93)]
        asks = [_b(0.958), _b(0.99)]
        lo, hi, book = fine_reward_display_lo_hi(
            0.9565,
            0.035,
            0.001,
            bids,
            asks,
        )
        self.assertTrue(book)
        self.assertAlmostEqual(lo, 0.923, places=6)
        self.assertAlmostEqual(hi, 0.99, places=6)

    def test_buy_half_band_not_asks_beyond_mid(self) -> None:
        """BUY reward display: band [mid−δ, mid] on bids only; no ask at mid+δ."""
        mid, delta = 0.0265, 0.035
        bids = [_b(0.001), _b(0.026)]
        asks = [_b(0.06)]
        lo, hi, book = fine_reward_display_lo_hi(
            mid,
            delta,
            0.001,
            bids,
            asks,
            side="BUY",
        )
        self.assertTrue(book)
        self.assertAlmostEqual(lo, 0.001, places=6)
        self.assertAlmostEqual(hi, 0.026, places=6)

    def test_buy_half_band_theory_snap(self) -> None:
        mid, delta = 0.0265, 0.035
        lo, hi, book = fine_reward_display_lo_hi(
            mid,
            delta,
            0.001,
            [],
            [],
            side="BUY",
        )
        self.assertFalse(book)
        self.assertAlmostEqual(lo, 0.001, places=6)
        self.assertAlmostEqual(hi, 0.026, places=6)

    def test_display_decimals(self) -> None:
        self.assertEqual(fine_tick_display_decimals(0.001), 3)
        self.assertEqual(fine_tick_display_decimals(0.01), 2)


if __name__ == "__main__":
    unittest.main()

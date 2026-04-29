"""Regression: custom coarse N ranks only order-book prices in the reward half-band."""

from __future__ import annotations

import unittest

from passive_liquidity.simple_price_policy import (
    CustomPricingSettings,
    _decide_custom_coarse,
    _ticks_from_mid_into_band,
    list_coarse_reward_tick_levels,
)


def _bids(*prices: float) -> list[dict]:
    return [{"price": p, "size": 1.0} for p in prices]


def _asks(*prices: float) -> list[dict]:
    return [{"price": p, "size": 1.0} for p in prices]


class TestCustomCoarseOffset(unittest.TestCase):
    def test_coarse_theory_levels_display(self) -> None:
        lo, hi, lv = list_coarse_reward_tick_levels(
            side="BUY",
            mid=0.6650,
            delta=0.0350,
            tick=0.01,
        )
        self.assertAlmostEqual(lo, 0.6350, places=6)
        self.assertAlmostEqual(hi, 0.6650, places=6)
        self.assertEqual(lv, [0.64, 0.65, 0.66])

    def test_ticks_from_mid_grid_stable(self) -> None:
        self.assertEqual(_ticks_from_mid_into_band("BUY", 0.16, 0.15, 0.01), 1)
        self.assertEqual(_ticks_from_mid_into_band("BUY", 0.16, 0.16, 0.01), 0)

    def test_n1_targets_nearest_book_level_buy(self) -> None:
        settings = CustomPricingSettings(
            coarse_tick_offset_from_mid=1,
            coarse_allow_top_of_book=True,
            coarse_min_candidate_levels=1,
            fine_safe_band_min=0.4,
            fine_safe_band_max=0.6,
            fine_target_band_ratio=0.5,
        )
        meta: dict = {}
        d, m = _decide_custom_coarse(
            side_u="BUY",
            price=0.20,
            mid=0.16,
            tick=0.01,
            delta=0.05,
            bids=_bids(0.11, 0.12, 0.13, 0.14, 0.15, 0.16),
            asks=[],
            min_replace_ticks=1,
            settings=settings,
            best_bid=0.10,
            best_ask=0.20,
            meta=meta,
        )
        self.assertEqual(d.action, "replace")
        self.assertAlmostEqual(float(d.new_price or 0), 0.16, places=6)
        self.assertEqual(m.get("custom_coarse_tick_offset_effective"), 1)

    def test_n_through_n4_buy_ladder(self) -> None:
        bids = _bids(0.11, 0.12, 0.13, 0.14, 0.15, 0.16)
        for n, want in [(4, 0.13), (3, 0.14), (2, 0.15), (1, 0.16)]:
            settings = CustomPricingSettings(
                coarse_tick_offset_from_mid=n,
                coarse_allow_top_of_book=True,
                coarse_min_candidate_levels=1,
                fine_safe_band_min=0.4,
                fine_safe_band_max=0.6,
                fine_target_band_ratio=0.5,
            )
            d, m = _decide_custom_coarse(
                side_u="BUY",
                price=0.20,
                mid=0.16,
                tick=0.01,
                delta=0.05,
                bids=bids,
                asks=[],
                min_replace_ticks=1,
                settings=settings,
                best_bid=0.10,
                best_ask=0.20,
                meta={},
            )
            self.assertEqual(
                d.action,
                "replace",
                msg=f"N={n} reason={m.get('reason_code')}",
            )
            self.assertAlmostEqual(float(d.new_price or 0), want, places=6, msg=f"N={n}")

    def test_mid_0285_rank_2_is_027(self) -> None:
        settings = CustomPricingSettings(
            coarse_tick_offset_from_mid=2,
            coarse_allow_top_of_book=True,
            coarse_min_candidate_levels=1,
            fine_safe_band_min=0.4,
            fine_safe_band_max=0.6,
            fine_target_band_ratio=0.5,
        )
        d, _ = _decide_custom_coarse(
            side_u="BUY",
            price=0.28,
            mid=0.285,
            tick=0.01,
            delta=0.035,
            bids=_bids(0.26, 0.27, 0.28),
            asks=[],
            min_replace_ticks=1,
            settings=settings,
            best_bid=0.10,
            best_ask=0.20,
            meta={},
        )
        self.assertEqual(d.action, "replace")
        self.assertAlmostEqual(float(d.new_price or 0), 0.27, places=6)

    def test_sparse_book_skips_empty_tick_rung_buy(self) -> None:
        """Theoretical band may include 0.88 but only 0.85–0.87 rest on book; N=1 is 0.87."""
        settings = CustomPricingSettings(
            coarse_tick_offset_from_mid=1,
            coarse_allow_top_of_book=True,
            coarse_min_candidate_levels=1,
            fine_safe_band_min=0.4,
            fine_safe_band_max=0.6,
            fine_target_band_ratio=0.5,
        )
        d, m = _decide_custom_coarse(
            side_u="BUY",
            price=0.84,
            mid=0.90,
            tick=0.01,
            delta=0.05,
            bids=_bids(0.85, 0.86, 0.87),
            asks=[],
            min_replace_ticks=1,
            settings=settings,
            best_bid=0.84,
            best_ask=0.91,
            meta={},
        )
        self.assertEqual(d.action, "replace")
        self.assertAlmostEqual(float(d.new_price or 0), 0.87, places=6)
        self.assertEqual(m.get("candidate_prices"), [0.87, 0.86, 0.85])

    def test_empty_book_insufficient(self) -> None:
        settings = CustomPricingSettings(
            coarse_tick_offset_from_mid=1,
            coarse_allow_top_of_book=True,
            coarse_min_candidate_levels=1,
            fine_safe_band_min=0.4,
            fine_safe_band_max=0.6,
            fine_target_band_ratio=0.5,
        )
        d, m = _decide_custom_coarse(
            side_u="BUY",
            price=0.20,
            mid=0.16,
            tick=0.01,
            delta=0.05,
            bids=[],
            asks=[],
            min_replace_ticks=1,
            settings=settings,
            best_bid=0.10,
            best_ask=0.20,
            meta={},
        )
        self.assertEqual(d.action, "keep")
        self.assertEqual(m.get("reason_code"), "custom_coarse_keep_insufficient_candidates")

    def test_sell_n1_nearest_mid_on_asks(self) -> None:
        settings = CustomPricingSettings(
            coarse_tick_offset_from_mid=1,
            coarse_allow_top_of_book=True,
            coarse_min_candidate_levels=1,
            fine_safe_band_min=0.4,
            fine_safe_band_max=0.6,
            fine_target_band_ratio=0.5,
        )
        # mid=0.50, SELL half-band up: lo=0.50 hi=0.55 with delta=0.05, tick 0.01 -> band 0.05
        d, m = _decide_custom_coarse(
            side_u="SELL",
            price=0.45,
            mid=0.50,
            tick=0.01,
            delta=0.05,
            bids=[],
            asks=_asks(0.50, 0.51, 0.52, 0.53, 0.54, 0.55),
            min_replace_ticks=1,
            settings=settings,
            best_bid=0.48,
            best_ask=0.52,
            meta={},
        )
        self.assertEqual(d.action, "replace")
        self.assertAlmostEqual(float(d.new_price or 0), 0.50, places=6)
        self.assertEqual(m.get("candidate_prices")[:3], [0.50, 0.51, 0.52])


if __name__ == "__main__":
    unittest.main()

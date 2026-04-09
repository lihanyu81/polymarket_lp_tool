"""resolve_effective_tick_size: API coarse + L2 gap/sub-cent → fine (0.001)."""

from __future__ import annotations

import unittest

from passive_liquidity.orderbook_fetcher import resolve_effective_tick_size


class TestResolveEffectiveTickSize(unittest.TestCase):
    def test_api_01_sub_cent_level_forces_001(self) -> None:
        bids = [{"price": "0.958", "size": "1"}]
        asks = [{"price": "0.959", "size": "1"}]
        t = resolve_effective_tick_size(0.01, bids, asks)
        self.assertAlmostEqual(t, 0.001, places=6)

    def test_api_01_min_gap_001_forces_001(self) -> None:
        bids = [
            {"price": "0.940", "size": "1"},
            {"price": "0.941", "size": "1"},
        ]
        asks = [{"price": "0.950", "size": "1"}]
        t = resolve_effective_tick_size(0.01, bids, asks)
        self.assertAlmostEqual(t, 0.001, places=6)

    def test_pure_cent_book_keeps_01(self) -> None:
        bids = [{"price": "0.94", "size": "1"}, {"price": "0.93", "size": "1"}]
        asks = [{"price": "0.96", "size": "1"}]
        t = resolve_effective_tick_size(0.01, bids, asks)
        self.assertAlmostEqual(t, 0.01, places=6)


if __name__ == "__main__":
    unittest.main()

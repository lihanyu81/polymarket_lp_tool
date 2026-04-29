from __future__ import annotations

from pathlib import Path
from typing import Any

from py_clob_client_v2 import ClobClient

from passive_liquidity.clob_factory import build_trading_client, funder_address
from passive_liquidity.config_manager import PassiveConfig
from passive_liquidity.custom_pricing_rules_store import CustomPricingRulesStore
from passive_liquidity.market_display import MarketDisplayResolver
from passive_liquidity.order_manager import OrderManager
from passive_liquidity.orderbook_fetcher import OrderBookFetcher
from passive_liquidity.reward_monitor import RewardMonitor
from passive_liquidity.telegram_notifier import build_telegram_notifier_from_env


class WebPanelContext:
    """Shared CLOB + helpers (one instance per process; matches main_loop wiring)."""

    def __init__(self) -> None:
        self._config = PassiveConfig.from_env()
        self._client = build_trading_client(
            self._config.clob_host, self._config.chain_id
        )
        self._ro_client = ClobClient(
            self._config.clob_host, chain_id=self._config.chain_id
        )
        self._order_manager = OrderManager()
        self._book_fetcher = OrderBookFetcher(self._ro_client)
        self._reward_monitor = RewardMonitor(self._config)
        self._market_display = MarketDisplayResolver(self._config.gamma_api_host)
        self._funder = funder_address()
        self._rules_store = CustomPricingRulesStore(
            Path(self._config.custom_rules_store_path)
        )
        tg = build_telegram_notifier_from_env()
        self._account_label = tg.account_label

    @property
    def config(self) -> PassiveConfig:
        return self._config

    @property
    def client(self) -> Any:
        return self._client

    @property
    def order_manager(self) -> OrderManager:
        return self._order_manager

    @property
    def book_fetcher(self) -> OrderBookFetcher:
        return self._book_fetcher

    @property
    def reward_monitor(self) -> RewardMonitor:
        return self._reward_monitor

    @property
    def market_display(self) -> MarketDisplayResolver:
        return self._market_display

    @property
    def funder(self) -> str:
        return self._funder

    @property
    def account_label(self) -> str:
        return self._account_label

    @property
    def rules_store(self) -> CustomPricingRulesStore:
        return self._rules_store

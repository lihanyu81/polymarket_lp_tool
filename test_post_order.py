#!/usr/bin/env python3
"""
Post a single limit order to Polymarket CLOB (same .env as the bot).

Example:
  cd polymarket_lp_tool && ./.venv/bin/python test_post_order.py \\
    --token-id 46118971458650555165410440115606491822058595161731979755989518360335087029561 \\
    --side BUY --price 0.01 --size 5

Use --dry-run to only build+sign without POST.
"""

from __future__ import annotations

import argparse
import json
import sys

from passive_liquidity.clob_factory import build_trading_client
from passive_liquidity.config_manager import PassiveConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Test post one GTC limit order to Polymarket CLOB.")
    parser.add_argument("--token-id", required=True, help="Outcome token_id (asset_id)")
    parser.add_argument("--side", choices=("BUY", "SELL"), required=True)
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--size", type=float, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create/sign order only; do not call POST /order",
    )
    parser.add_argument(
        "--not-post-only",
        action="store_true",
        help="Allow immediate match (default: post_only=True)",
    )
    args = parser.parse_args()

    config = PassiveConfig.from_env()
    client = build_trading_client(config.clob_host, config.chain_id)

    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    side_const = BUY if args.side == "BUY" else SELL
    post_only = not args.not_post_only

    try:
        signed = client.create_order(
            OrderArgs(
                token_id=args.token_id,
                price=float(args.price),
                size=float(args.size),
                side=side_const,
            ),
            PartialCreateOrderOptions(),
        )
    except Exception as e:
        print("create_order failed:", e, file=sys.stderr)
        return 1

    if args.dry_run:
        print("dry-run OK (signed order built, not posted)")
        print(signed)
        return 0

    try:
        resp = client.post_order(signed, order_type=OrderType.GTC, post_only=post_only)
    except Exception as e:
        print("post_order failed:", e, file=sys.stderr)
        return 1

    print("post_order OK:")
    if isinstance(resp, dict):
        print(json.dumps(resp, indent=2))
    else:
        print(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

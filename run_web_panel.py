#!/usr/bin/env python3
"""
Browser control panel: status, orders (+ reward hints), cancel, PnL, custom rules JSON.

Requires:
  pip install -r requirements.txt
  WEB_PANEL_TOKEN in .env (login password; keep secret)

Run (default http://127.0.0.1:8765):
  python run_web_panel.py

Env:
  WEB_PANEL_HOST   default 127.0.0.1
  WEB_PANEL_PORT   default 8765
  WEB_PANEL_SECRET_KEY  optional Flask session signing key
"""

from passive_liquidity.web_panel.app import main

if __name__ == "__main__":
    main()

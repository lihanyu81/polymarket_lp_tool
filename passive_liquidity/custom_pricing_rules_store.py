"""
Persist per-(token_id, side) custom pricing rules (JSON).

Stable key: ``f"{token_id}:{SIDE}"`` with SIDE in BUY|SELL.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from passive_liquidity.simple_price_policy import CustomPricingSettings

LOG = logging.getLogger(__name__)

CustomRuleRegime = Literal["coarse", "fine"]


def stable_rule_key(token_id: str, side: str) -> str:
    return f"{str(token_id).strip()}:{str(side).strip().upper()}"


@dataclass
class StoredCustomRule:
    tick_regime: CustomRuleRegime
    coarse_tick_offset_from_mid: int
    coarse_allow_top_of_book: bool
    coarse_min_candidate_levels: int
    fine_safe_band_min: float
    fine_safe_band_max: float
    fine_target_band_ratio: float

    def to_settings(self) -> CustomPricingSettings:
        return CustomPricingSettings(
            coarse_tick_offset_from_mid=int(self.coarse_tick_offset_from_mid),
            coarse_allow_top_of_book=bool(self.coarse_allow_top_of_book),
            coarse_min_candidate_levels=int(self.coarse_min_candidate_levels),
            fine_safe_band_min=float(self.fine_safe_band_min),
            fine_safe_band_max=float(self.fine_safe_band_max),
            fine_target_band_ratio=float(self.fine_target_band_ratio),
        )


class CustomPricingRulesStore:
    """Thread-safe in-memory rules with JSON persistence.

    The bot and the web panel are separate processes: both read/write the same
    file path. On each ``get_rule`` / ``list_keys``, we reload from disk when
    the file's mtime changed so web (or Telegram in another process) edits take
    effect without restarting the bot.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._rules: dict[str, dict[str, Any]] = {}
        self._disk_mtime: Optional[float] = None
        self._load_unlocked()
        if self._path.is_file():
            try:
                self._disk_mtime = self._path.stat().st_mtime
            except OSError:
                self._disk_mtime = None

    @property
    def path(self) -> Path:
        return self._path

    def _sync_from_disk_if_changed_unlocked(self) -> None:
        """Reload JSON if the file changed on disk (call with ``_lock`` held)."""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._rules = {}
            self._disk_mtime = None
            return
        if self._disk_mtime is not None and mtime <= self._disk_mtime:
            return
        self._load_unlocked()
        LOG.info("reloaded custom pricing rules from disk: %s", self._path)
        try:
            self._disk_mtime = self._path.stat().st_mtime
        except OSError:
            self._disk_mtime = None

    def _load_unlocked(self) -> None:
        if not self._path.is_file():
            self._rules = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            LOG.warning("custom rules load failed %s: %s", self._path, e)
            self._rules = {}
            return
        rules = raw.get("rules")
        self._rules = dict(rules) if isinstance(rules, dict) else {}

    def _persist_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "rules": self._rules}
        data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(
            suffix=".json", dir=str(self._path.parent or "."), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get_rule(self, token_id: str, side: str) -> Optional[StoredCustomRule]:
        key = stable_rule_key(token_id, side)
        with self._lock:
            self._sync_from_disk_if_changed_unlocked()
            row = self._rules.get(key)
            if not isinstance(row, dict):
                return None
            try:
                return StoredCustomRule(
                    tick_regime=(
                        "coarse" if row.get("tick_regime") == "coarse" else "fine"
                    ),
                    coarse_tick_offset_from_mid=int(
                        row["coarse_tick_offset_from_mid"]
                    ),
                    coarse_allow_top_of_book=bool(row["coarse_allow_top_of_book"]),
                    coarse_min_candidate_levels=int(
                        row["coarse_min_candidate_levels"]
                    ),
                    fine_safe_band_min=float(row["fine_safe_band_min"]),
                    fine_safe_band_max=float(row["fine_safe_band_max"]),
                    fine_target_band_ratio=float(row["fine_target_band_ratio"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                LOG.warning("bad custom rule row key=%s: %s", key, e)
                return None

    def set_rule(self, token_id: str, side: str, rule: StoredCustomRule) -> None:
        key = stable_rule_key(token_id, side)
        row = {
            "tick_regime": rule.tick_regime,
            **{k: v for k, v in asdict(rule).items() if k != "tick_regime"},
        }
        with self._lock:
            self._rules[key] = row
            self._persist_unlocked()
            try:
                self._disk_mtime = self._path.stat().st_mtime
            except OSError:
                self._disk_mtime = None
        LOG.info("custom rule saved key=%s regime=%s", key, rule.tick_regime)

    def clear_rule(self, token_id: str, side: str) -> bool:
        key = stable_rule_key(token_id, side)
        with self._lock:
            if key not in self._rules:
                return False
            del self._rules[key]
            self._persist_unlocked()
            try:
                self._disk_mtime = self._path.stat().st_mtime
            except OSError:
                self._disk_mtime = None
        LOG.info("custom rule cleared key=%s", key)
        return True

    def list_keys(self) -> list[str]:
        with self._lock:
            self._sync_from_disk_if_changed_unlocked()
            return sorted(self._rules.keys())

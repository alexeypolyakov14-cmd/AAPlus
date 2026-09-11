"""Настройки системы: YAML-файл + переменные окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "data": {
        "boards": ["TQOB", "TQCB"],
        "cache_path": "data/cache/http_cache.sqlite",
        "keyrate_from": "2013-09-13",
        "ratings_csv": "data/ratings.csv",
        "ratings_conservative": True,
    },
    "screener": {
        "currency": "SUR",
        "min_ytm": 0.0,
        "max_ytm": 40.0,
        "min_duration": 0.2,
        "max_duration": 12.0,
        "min_turnover": 1_000_000,
        "max_list_level": 2,
        "exclude_floaters": True,
        "exclude_linkers": True,
        "exclude_amortization": False,
        "exclude_offers": False,
        "max_bid_ask_pct": 1.0,
        "max_g_spread_bp": 1500,
        "min_price": 50.0,
        "issuer_blacklist": [],
        "min_rating": "",
        "require_rating": False,
    },
    "risk": {
        "max_weight_per_bond": 0.10,
        "max_weight_per_bond_ofz": 0.35,
        "max_weight_per_issuer": 0.20,
        "max_corporate_share": 0.60,
        "min_portfolio_duration": 0.5,
        "max_portfolio_duration": 6.0,
        "max_g_spread_bp": 800,
        "max_turnover_share": 0.05,
        "yield_vol_bp_daily": 15.0,
        "max_unrated_share": 1.0,
        "min_rating": "",
    },
    "strategy": {
        "name": "carry",
        "params": {},
        "rebalance": "monthly",
    },
    "backtest": {
        "commission_bp": 5,
        "slippage_bp": 5,
        "initial_cash": 1_000_000,
        "benchmark": "RGBITR",
        "cash_spread_bp": -50,
    },
    "execution": {
        "broker": "paper",
        "state_path": "state/portfolio.json",
        "journal_path": "state/orders.jsonl",
        "tinvest": {
            "sandbox": True,
            "account_id": "",
            "token_env": "TINVEST_TOKEN",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Settings:
    raw: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG))

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Settings":
        cfg = dict(DEFAULT_CONFIG)
        path = path or os.environ.get("BONDTRADER_CONFIG", "config.yaml")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            cfg = _deep_merge(cfg, user)
        return cls(cfg)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def tinvest_token(self) -> str:
        env = self.get("execution", "tinvest", "token_env", default="TINVEST_TOKEN")
        return os.environ.get(env, "")

    def dump(self) -> str:
        return yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=False)

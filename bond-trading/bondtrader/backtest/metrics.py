"""Метрики доходности/риска для рядов NAV."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 250


@dataclass
class PerformanceStats:
    start: str
    end: str
    days: int
    total_return: float        # %
    cagr: float                # %
    volatility: float          # % годовых
    sharpe: float
    sortino: float
    max_drawdown: float        # % (отрицательное число)
    calmar: float
    best_day: float
    worst_day: float
    benchmark_total_return: Optional[float] = None
    excess_return: Optional[float] = None       # п.п. (CAGR − CAGR бенчмарка)
    beta: Optional[float] = None
    tracking_error: Optional[float] = None
    information_ratio: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


def drawdown_series(nav: pd.Series) -> pd.Series:
    peak = nav.cummax()
    return nav / peak - 1.0


def performance(nav: pd.Series, rf_annual_pct: float = 0.0, benchmark: Optional[pd.Series] = None) -> PerformanceStats:
    nav = nav.dropna()
    if len(nav) < 2:
        raise ValueError("слишком короткий ряд NAV")
    rets = nav.pct_change().dropna()
    n_days = (nav.index[-1] - nav.index[0]).days
    years = max(n_days / 365.25, 1e-9)
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else total
    vol = rets.std(ddof=1) * math.sqrt(TRADING_DAYS) if len(rets) > 1 else 0.0
    rf_daily = (1 + rf_annual_pct / 100) ** (1 / TRADING_DAYS) - 1
    excess = rets - rf_daily
    sharpe = excess.mean() / rets.std(ddof=1) * math.sqrt(TRADING_DAYS) if rets.std(ddof=1) > 0 else 0.0
    downside = rets[rets < rf_daily] - rf_daily
    dd_std = math.sqrt((downside ** 2).sum() / len(rets)) if len(rets) else 0.0
    sortino = excess.mean() / dd_std * math.sqrt(TRADING_DAYS) if dd_std > 0 else 0.0
    mdd = drawdown_series(nav).min()
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0
    stats = PerformanceStats(
        start=str(nav.index[0].date()), end=str(nav.index[-1].date()), days=n_days,
        total_return=total * 100, cagr=cagr * 100, volatility=vol * 100, sharpe=sharpe, sortino=sortino,
        max_drawdown=mdd * 100, calmar=calmar, best_day=rets.max() * 100, worst_day=rets.min() * 100,
    )
    if benchmark is not None and len(benchmark.dropna()) > 2:
        b = benchmark.reindex(nav.index).ffill().dropna()
        common = nav.index.intersection(b.index)
        if len(common) > 2:
            b = b.loc[common]
            nv = nav.loc[common]
            b_total = b.iloc[-1] / b.iloc[0] - 1
            b_cagr = (1 + b_total) ** (1 / years) - 1
            br = b.pct_change().dropna()
            pr = nv.pct_change().dropna()
            idx = br.index.intersection(pr.index)
            br, pr = br.loc[idx], pr.loc[idx]
            cov = np.cov(pr.values, br.values, ddof=1) if len(idx) > 2 else np.zeros((2, 2))
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else 0.0
            diff = pr - br
            te = diff.std(ddof=1) * math.sqrt(TRADING_DAYS) if len(diff) > 1 else 0.0
            ir = (diff.mean() * TRADING_DAYS) / te if te > 0 else 0.0
            stats.benchmark_total_return = b_total * 100
            stats.excess_return = (cagr - b_cagr) * 100
            stats.beta = beta
            stats.tracking_error = te * 100
            stats.information_ratio = ir
    return stats

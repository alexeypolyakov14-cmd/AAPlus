"""Провайдеры исторических данных для бэктеста."""
from __future__ import annotations

from datetime import date
from typing import Optional, Protocol

import pandas as pd

from ..data.cbr import fetch_keyrate_history
from ..data.moex import MoexClient, apply_bondization
from ..models import Bond


class HistoryProvider(Protocol):
    def bond_history(self, bond: Bond, start: date, end: date) -> pd.DataFrame: ...
    def index_history(self, index: str, start: date, end: date) -> pd.Series: ...
    def keyrate_history(self, start: date, end: date) -> list[tuple[date, float]]: ...
    def enrich(self, bond: Bond) -> Bond: ...
    def zcyc(self, on: date) -> Optional[dict]: ...


HIST_COLUMNS = ["close", "ytm", "duration", "accrued", "face", "value"]


class MoexHistoryProvider:
    """История с MOEX ISS + ключевая ставка ЦБ."""

    def __init__(self, client: MoexClient, cache=None, load_curves: bool = False):
        self.client = client
        self.cache = cache
        self.load_curves = load_curves

    def bond_history(self, bond: Bond, start: date, end: date) -> pd.DataFrame:
        rows = self.client.history(bond.secid, bond.board or "TQOB", start, end)
        if not rows:
            return pd.DataFrame(columns=HIST_COLUMNS)
        df = pd.DataFrame(rows).set_index("date")
        df.index = pd.to_datetime(df.index)
        return df[HIST_COLUMNS]

    def index_history(self, index: str, start: date, end: date) -> pd.Series:
        rows = self.client.index_history(index, start, end)
        s = pd.Series({pd.Timestamp(d): v for d, v in rows}, dtype=float).sort_index()
        s.name = index
        return s

    def keyrate_history(self, start: date, end: date) -> list[tuple[date, float]]:
        return fetch_keyrate_history(start, end, cache=self.cache)

    def enrich(self, bond: Bond) -> Bond:
        return self.client.enrich(bond)

    def zcyc(self, on: date) -> Optional[dict]:
        if not self.load_curves:
            return None
        try:
            return self.client.zcyc(on)
        except Exception:  # noqa: BLE001
            return None


class FrameHistoryProvider:
    """Офлайн-провайдер из готовых DataFrame'ов (тесты, CSV-выгрузки).

    histories: secid -> DataFrame(index=DatetimeIndex, columns=HIST_COLUMNS)
    """

    def __init__(self, histories: dict[str, pd.DataFrame], index: Optional[pd.Series] = None,
                 keyrate: Optional[list[tuple[date, float]]] = None, schedules: Optional[dict[str, dict]] = None):
        self.histories = histories
        self.index = index
        self.keyrate = keyrate or []
        self.schedules = schedules or {}

    def bond_history(self, bond: Bond, start: date, end: date) -> pd.DataFrame:
        df = self.histories.get(bond.secid)
        if df is None:
            return pd.DataFrame(columns=HIST_COLUMNS)
        return df.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def index_history(self, index: str, start: date, end: date) -> pd.Series:
        if self.index is None:
            return pd.Series(dtype=float)
        return self.index.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def keyrate_history(self, start: date, end: date) -> list[tuple[date, float]]:
        return [(d, r) for d, r in self.keyrate if start <= d <= end]

    def enrich(self, bond: Bond) -> Bond:
        sched = self.schedules.get(bond.secid)
        return apply_bondization(bond, sched) if sched else bond

    def zcyc(self, on: date) -> Optional[dict]:
        return None


def load_csv_histories(path_pattern: str, secids: list[str]) -> dict[str, pd.DataFrame]:
    """CSV с колонками date,close,ytm,duration,accrued,face,value; path_pattern содержит {secid}."""
    out = {}
    for s in secids:
        df = pd.read_csv(path_pattern.format(secid=s), parse_dates=["date"]).set_index("date").sort_index()
        for c in HIST_COLUMNS:
            if c not in df.columns:
                df[c] = None
        out[s] = df[HIST_COLUMNS]
    return out

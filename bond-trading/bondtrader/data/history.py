"""История G-спредов с MOEX ISS: дневные доходности бумаги против кривой ОФЗ на ту же дату.

Спред за прошлую дату = YIELDCLOSE (доходность по цене закрытия, MOEX) − zcyc(дата).yield_at(DURATION MOEX).
Чтобы «сегодня» было сопоставимо с историей, текущая точка считается в той же методике: биржевые YIELD и
DURATION из marketdata против сегодняшней кривой (наш собственный YTW/G-спред остаётся в метриках бумаги).

Кривые по датам складываются в книгу data/zcyc_history.json (одна кривая на торговый день, ~20 точек) —
в CI она едет в кэш вместе с остальными книгами, чтобы не тянуть 60 кривых на каждый запуск.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Optional

from ..analytics.curve import ZeroCurve
from ..analytics.history import SpreadStats, spread_stats
from ..models import Bond, Quote

log = logging.getLogger(__name__)


class ZcycStore:
    """Книга кривых ОФЗ по датам: {ISO-дата: [[тенор, доходность], ...]}."""

    def __init__(self, path: str = "data/zcyc_history.json"):
        self.path = path
        self.curves: dict[str, list] = {}
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.curves = dict(json.load(f))   # [] — на эту дату кривой нет (выходной/праздник), чтобы не спрашивать MOEX снова
            except (OSError, ValueError) as e:
                log.warning("книга кривых %s не прочитана: %s", path, e)

    def known(self, on: date) -> bool:
        return on.isoformat() in self.curves

    def get(self, on: date) -> Optional[ZeroCurve]:
        pts = self.curves.get(on.isoformat())
        return ZeroCurve(on, [(t, y) for t, y in pts], source="moex_zcyc") if pts else None

    def put(self, curve: Optional[ZeroCurve], on: date) -> None:
        """curve=None — MOEX подтвердил, что на эту дату кривой нет (ответил кривой другой даты)."""
        self.curves[on.isoformat()] = [[t, y] for t, y in curve.points] if curve is not None and curve.points else []
        self.dirty = True

    def save(self) -> None:
        if not self.dirty or not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(self.curves.items())), f, ensure_ascii=False)
        self.dirty = False


class SpreadHistoryService:
    """Считает историю спредов по бумаге лениво и запоминает результат на время процесса."""

    def __init__(self, client, settle: date, days: int = 90, store: Optional[ZcycStore] = None, today_curve: Optional[ZeroCurve] = None):
        self.client = client
        self.settle = settle
        self.days = days
        self.store = store or ZcycStore("")
        self.today_curve = today_curve
        self._curves: dict[date, Optional[ZeroCurve]] = {}
        self._series: dict[str, list[tuple[date, float]]] = {}
        self._stats: dict[str, Optional[SpreadStats]] = {}
        self.failures = 0

    # --- кривая на дату ---
    def curve_on(self, on: date) -> Optional[ZeroCurve]:
        if on in self._curves:
            return self._curves[on]
        curve = self.store.get(on)
        if curve is None and not self.store.known(on):
            try:
                curve = ZeroCurve.from_moex_zcyc(self.client.zcyc(on))
                # MOEX отдаёт последнюю доступную кривую; если её дата не совпала с запрошенной — на этот день кривой нет
                if curve.trade_date is not None and curve.trade_date != on:
                    curve = None
                self.store.put(curve, on)      # и отсутствие тоже запоминаем — это факт календаря, а не сбой
            except Exception as e:  # noqa: BLE001 — недоступность: точка пропускается, но в книгу не пишем (может быть сбой сети)
                log.debug("zcyc %s: %s", on, e)
                curve = None
        self._curves[on] = curve
        return curve

    # --- история спреда бумаги ---
    def series(self, bond: Bond) -> list[tuple[date, float]]:
        if bond.secid in self._series:
            return self._series[bond.secid]
        pts: list[tuple[date, float]] = []
        try:
            rows = self.client.history(bond.secid, bond.board or "TQCB", self.settle - timedelta(days=self.days), self.settle)
        except Exception as e:  # noqa: BLE001
            log.warning("история %s: %s", bond.secid, e)
            rows, self.failures = [], self.failures + 1
        for r in rows:
            ytm, dur = r.get("ytm"), r.get("duration")
            if ytm is None or dur is None or dur <= 0 or r["date"] >= self.settle:
                continue
            curve = self.curve_on(r["date"])
            if curve is None:
                continue
            pts.append((r["date"], (ytm - curve.yield_at(dur)) * 100.0))
        pts.sort()
        self._series[bond.secid] = pts
        return pts

    def now_spread(self, quote: Quote, fallback: Optional[float]) -> Optional[float]:
        """Текущий спред в методике истории (биржевые YIELD/DURATION против сегодняшней кривой), иначе наш G-спред."""
        if self.today_curve is not None and quote.ytm_moex and quote.duration_moex:
            return (quote.ytm_moex - self.today_curve.yield_at(quote.duration_moex)) * 100.0
        return fallback

    def stats(self, bond: Bond, quote: Quote, fallback_spread: Optional[float]) -> Optional[SpreadStats]:
        if bond.secid in self._stats:
            return self._stats[bond.secid]
        st = spread_stats(self.series(bond), self.now_spread(quote, fallback_spread), self.settle, self.days)
        self._stats[bond.secid] = st
        return st

    def save(self) -> None:
        self.store.save()

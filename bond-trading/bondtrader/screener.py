"""Скринер облигаций: фильтры качества/ликвидности + расчёт метрик + скоринг."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import pandas as pd

from .analytics.bond_math import compute_metrics
from .analytics.curve import ZeroCurve
from .data.ratings import Rating, RatingsBook, rating_at_least
from .models import Bond, BondMetrics, Quote


@dataclass
class ScreenRow:
    bond: Bond
    quote: Quote
    metrics: BondMetrics
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    rating: Optional[Rating] = None

    @property
    def secid(self) -> str:
        return self.bond.secid

    @property
    def rating_str(self) -> str:
        return f"{self.rating.rating} ({self.rating.agency})" if self.rating else "—"

    def as_dict(self) -> dict:
        m = self.metrics
        return {
            "secid": self.bond.secid, "name": self.bond.name, "board": self.bond.board,
            "ofz": self.bond.is_ofz, "level": self.bond.list_level, "price": m.clean_price,
            "ytm": round(m.ytm, 2), "ytm_offer": None if m.ytm_to_offer is None else round(m.ytm_to_offer, 2),
            "yield_worst": round(m.yield_worst, 2), "duration": round(m.macaulay_duration, 2),
            "mod_dur": round(m.modified_duration, 2), "g_spread": None if m.g_spread is None else round(m.g_spread),
            "cur_yield": round(m.current_yield, 2), "years": round(m.years_to_maturity, 2),
            "turnover_mln": round(self.quote.turnover / 1e6, 1), "bid_ask_pct": self.quote.bid_ask_spread_pct,
            "maturity": self.bond.maturity, "offer": self.bond.offer_date, "rating": self.rating_str,
            "score": round(self.score, 3), "flags": ",".join(self.flags),
        }


@dataclass
class ScreenerConfig:
    currency: str = "SUR"
    min_ytm: float = 0.0
    max_ytm: float = 40.0
    min_duration: float = 0.2
    max_duration: float = 12.0
    min_turnover: float = 1_000_000
    max_list_level: int = 2
    exclude_floaters: bool = True
    exclude_linkers: bool = True
    exclude_amortization: bool = False
    exclude_offers: bool = False
    max_bid_ask_pct: float = 1.0
    max_g_spread_bp: float = 1500
    min_price: float = 50.0
    issuer_blacklist: list[str] = field(default_factory=list)
    ofz_only: bool = False
    corporate_only: bool = False
    min_rating: str = ""             # напр. "BB-": бумаги с худшим рейтингом отсеиваются
    require_rating: bool = False     # без рейтинга — отсев (ОФЗ считаются AAA)

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenerConfig":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Screener:
    def __init__(self, cfg: Optional[ScreenerConfig] = None):
        self.cfg = cfg or ScreenerConfig()
        self.rejected: dict[str, str] = {}

    # ---- фильтры до расчёта метрик (дёшево) ----
    def _prefilter(self, bond: Bond, quote: Quote, settle: date) -> Optional[str]:
        c = self.cfg
        if quote.price is None or quote.price <= 0:
            return "нет цены"
        if bond.maturity is None or bond.maturity <= settle:
            return "погашена/нет даты погашения"
        if c.currency and bond.currency not in (c.currency, "RUB") and c.currency != "*":
            return f"валюта {bond.currency}"
        if c.ofz_only and not bond.is_ofz:
            return "не ОФЗ"
        if c.corporate_only and bond.is_ofz:
            return "ОФЗ"
        if c.exclude_floaters and bond.is_floater:
            return "флоатер"
        if c.exclude_linkers and bond.is_linker:
            return "линкер"
        if c.exclude_amortization and bond.has_amortization:
            return "амортизация"
        if c.exclude_offers and bond.has_offer and bond.offer_date and bond.offer_date > settle:
            return "оферта"
        if bond.list_level and c.max_list_level and bond.list_level > c.max_list_level:
            return f"листинг {bond.list_level}"
        if quote.turnover < c.min_turnover:
            return "низкий оборот"
        ba = quote.bid_ask_spread_pct
        if ba is not None and ba > c.max_bid_ask_pct:
            return "широкий bid/ask"
        if quote.price < c.min_price:
            return "цена ниже порога"
        if any(bl.upper() in (bond.name or "").upper() for bl in c.issuer_blacklist):
            return "эмитент в чёрном списке"
        return None

    def _postfilter(self, m: BondMetrics) -> Optional[str]:
        c = self.cfg
        if not (c.min_ytm <= m.yield_worst <= c.max_ytm):
            return f"доходность {m.yield_worst:.1f}% вне диапазона"
        if not (c.min_duration <= m.macaulay_duration <= c.max_duration):
            return f"дюрация {m.macaulay_duration:.1f} вне диапазона"
        if m.g_spread is not None and m.g_spread > c.max_g_spread_bp:
            return f"G-спред {m.g_spread:.0f} б.п. (дистресс)"
        return None

    def run(self, universe: list[tuple[Bond, Quote]], curve: Optional[ZeroCurve], settle: date,
            enrich: Optional[Callable[[Bond], Bond]] = None, ratings: Optional[RatingsBook] = None) -> list[ScreenRow]:
        """universe — пары (Bond, Quote); enrich — функция подгрузки графика (например MoexClient.enrich);
        ratings — книга рейтингов (ОФЗ считаются AAA)."""
        rows: list[ScreenRow] = []
        self.rejected = {}
        c = self.cfg
        for bond, quote in universe:
            why = self._prefilter(bond, quote, settle)
            if why:
                self.rejected[bond.secid] = why
                continue
            rating: Optional[Rating] = None
            if bond.is_ofz:
                rating = Rating("Минфин России", "—", "AAA", kind="issuer")
            elif ratings is not None:
                rating = ratings.lookup(bond)
            if c.min_rating or c.require_rating:
                if rating is None:
                    if c.require_rating:
                        self.rejected[bond.secid] = "нет рейтинга"
                        continue
                elif c.min_rating and not rating_at_least(rating.rating, c.min_rating):
                    self.rejected[bond.secid] = f"рейтинг {rating.rating} ниже {c.min_rating}"
                    continue
            if enrich is not None and not bond.has_full_schedule:
                bond = enrich(bond)  # полный график купонов/амортизаций/оферт — иначе YTM расходится с биржевым
            m = compute_metrics(bond, quote, settle, curve)
            if m is None:
                # fallback на биржевые данные, если наш расчёт невозможен
                if quote.ytm_moex and quote.duration_moex:
                    dirty = bond.face_value * quote.price / 100 + quote.accrued
                    mod = quote.duration_moex / (1 + quote.ytm_moex / 100)
                    gs = (quote.ytm_moex - curve.yield_at(quote.duration_moex)) * 100 if curve else None
                    m = BondMetrics(bond.secid, quote.price, dirty, quote.ytm_moex, None, quote.ytm_moex,
                                    quote.duration_moex, mod, 0.0, mod * dirty * 1e-4,
                                    (bond.maturity - settle).days / 365, 0.0, gs)
                else:
                    self.rejected[bond.secid] = "не удалось рассчитать метрики"
                    continue
            why = self._postfilter(m)
            if why:
                self.rejected[bond.secid] = why
                continue
            flags = []
            if bond.has_offer and bond.offer_date and bond.offer_date > settle:
                flags.append("оферта")
            if bond.has_amortization:
                flags.append("амортизация")
            if bond.is_floater:
                flags.append("флоатер")
            if quote.ytm_moex and abs(quote.ytm_moex - m.ytm) > 1.0:
                flags.append(f"расхождение с YTM MOEX {quote.ytm_moex:.1f}")
            if not bond.is_ofz and rating is None and ratings is not None:
                flags.append("без рейтинга")
            rows.append(ScreenRow(bond, quote, m, flags=flags, rating=rating))
        self._score(rows)
        rows.sort(key=lambda r: r.score, reverse=True)
        return rows

    @staticmethod
    def _score(rows: list[ScreenRow]) -> None:
        """Композитный скор: 0.5·z(доходность к худшему) + 0.3·z(G-спред) + 0.2·z(log оборота)."""
        if not rows:
            return

        def z(values: list[float]) -> list[float]:
            n = len(values)
            mean = sum(values) / n
            var = sum((v - mean) ** 2 for v in values) / n
            sd = math.sqrt(var) if var > 0 else 1.0
            return [(v - mean) / sd for v in values]

        zy = z([r.metrics.yield_worst for r in rows])
        zs = z([r.metrics.g_spread or 0.0 for r in rows])
        zl = z([math.log1p(r.quote.turnover) for r in rows])
        for r, a, b, c in zip(rows, zy, zs, zl):
            r.score = 0.5 * a + 0.3 * b + 0.2 * c


def to_dataframe(rows: list[ScreenRow]) -> pd.DataFrame:
    return pd.DataFrame([r.as_dict() for r in rows])


def build_curve(universe: list[tuple[Bond, Quote]], settle: date, zcyc_payload: Optional[dict] = None) -> ZeroCurve:
    """Кривая: из zcyc MOEX, а при его отсутствии — аппроксимация по ОФЗ-ПД из universe."""
    if zcyc_payload:
        try:
            return ZeroCurve.from_moex_zcyc(zcyc_payload)
        except ValueError:
            pass
    items = []
    for bond, q in universe:
        if not bond.is_ofz or bond.is_floater or bond.is_linker or q.price is None:
            continue
        m = compute_metrics(bond, q, settle)
        if m:
            items.append((m.macaulay_duration, m.ytm))
        elif q.ytm_moex and q.duration_moex:
            items.append((q.duration_moex, q.ytm_moex))
    return ZeroCurve.from_ofz_metrics(items, settle)

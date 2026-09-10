"""Риск-менеджмент: лимиты концентрации, дюрации, ликвидности; DV01 и параметрический VaR."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .portfolio import Order, Portfolio
from .screener import ScreenRow


@dataclass
class RiskLimits:
    max_weight_per_bond: float = 0.10
    max_weight_per_bond_ofz: float = 0.35   # для ОФЗ лимит мягче: нет кредитного риска эмитента
    max_weight_per_issuer: float = 0.20
    max_corporate_share: float = 0.60
    min_portfolio_duration: float = 0.5
    max_portfolio_duration: float = 6.0
    max_g_spread_bp: float = 800
    max_turnover_share: float = 0.05
    yield_vol_bp_daily: float = 15.0
    min_list_level: int = 2  # допускаем уровни 1..min_list_level

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Violation:
    code: str
    message: str
    secid: Optional[str] = None
    hard: bool = True   # hard — блокирует исполнение, soft — предупреждение


@dataclass
class PortfolioRisk:
    nav: float
    duration: float            # модифицированная, взвешенная по стоимости
    dv01: float                # руб. на 1 б.п.
    var_1d_95: float           # руб.
    var_10d_99: float
    corporate_share: float
    cash_share: float
    issuer_exposure: dict[str, float] = field(default_factory=dict)


class RiskManager:
    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()

    # ---- целевые веса ----
    def enforce_targets(self, targets: dict[str, float], rows: dict[str, ScreenRow]) -> tuple[dict[str, float], list[Violation]]:
        """Обрезает целевые веса по лимитам. Возвращает (веса, список замечаний)."""
        L = self.limits
        out: dict[str, float] = {}
        notes: list[Violation] = []
        for secid, w in targets.items():
            row = rows.get(secid)
            if row is None:
                notes.append(Violation("unknown", f"{secid}: нет в скрине, исключён", secid))
                continue
            if row.metrics.g_spread is not None and row.metrics.g_spread > L.max_g_spread_bp:
                notes.append(Violation("gspread", f"{secid}: G-спред {row.metrics.g_spread:.0f} б.п. > лимита", secid))
                continue
            if row.bond.list_level and row.bond.list_level > L.min_list_level:
                notes.append(Violation("listing", f"{secid}: уровень листинга {row.bond.list_level}", secid))
                continue
            cap = L.max_weight_per_bond_ofz if row.bond.is_ofz else L.max_weight_per_bond
            if w > cap:
                notes.append(Violation("bond_cap", f"{secid}: вес {w:.1%} обрезан до {cap:.0%}", secid, hard=False))
                w = cap
            out[secid] = max(w, 0.0)

        # лимит на эмитента
        by_issuer: dict[str, list[str]] = {}
        for secid in out:
            by_issuer.setdefault(rows[secid].bond.issuer_key, []).append(secid)
        for issuer, ids in by_issuer.items():
            if issuer == "МИНФИН":
                continue
            tot = sum(out[i] for i in ids)
            if tot > L.max_weight_per_issuer:
                k = L.max_weight_per_issuer / tot
                for i in ids:
                    out[i] *= k
                notes.append(Violation("issuer_cap", f"{issuer}: доля {tot:.1%} обрезана до {L.max_weight_per_issuer:.0%}", hard=False))

        # доля корпоративного сегмента
        corp = [s for s in out if not rows[s].bond.is_ofz]
        corp_share = sum(out[s] for s in corp)
        if corp_share > L.max_corporate_share:
            k = L.max_corporate_share / corp_share
            for s in corp:
                out[s] *= k
            notes.append(Violation("corp_share", f"доля корпоратов {corp_share:.1%} обрезана до {L.max_corporate_share:.0%}", hard=False))

        total = sum(out.values())
        if total > 1.0 + 1e-9:
            for s in out:
                out[s] /= total

        dur = sum(out[s] * rows[s].metrics.modified_duration for s in out)
        if out and dur > L.max_portfolio_duration:
            notes.append(Violation("duration", f"дюрация цели {dur:.2f} > лимита {L.max_portfolio_duration}", hard=False))
        if out and dur < L.min_portfolio_duration:
            notes.append(Violation("duration", f"дюрация цели {dur:.2f} < минимума {L.min_portfolio_duration}", hard=False))
        return out, notes

    # ---- ордера ----
    def check_orders(self, orders: list[Order], rows: dict[str, ScreenRow], portfolio: Portfolio, nav: float) -> list[Violation]:
        L = self.limits
        v: list[Violation] = []
        cash = portfolio.cash
        for o in orders:
            row = rows.get(o.secid)
            if row is None:
                v.append(Violation("unknown", f"{o.secid}: нет рыночных данных", o.secid))
                continue
            value = o.qty * row.metrics.dirty_price
            if row.quote.turnover > 0 and value > L.max_turnover_share * row.quote.turnover:
                v.append(Violation("liquidity", f"{o.secid}: ордер {value/1e6:.2f} млн > {L.max_turnover_share:.0%} дневного оборота", o.secid, hard=False))
            if o.side == "BUY":
                cash -= value
            else:
                pos = portfolio.positions.get(o.secid)
                if not pos or pos.qty < o.qty:
                    v.append(Violation("short", f"{o.secid}: продажа {o.qty} шт. без позиции", o.secid))
                cash += value
        if cash < -1e-6:
            v.append(Violation("cash", f"недостаточно денег: дефицит {-cash:,.0f} руб.", hard=True))
        return v

    # ---- статистика портфеля ----
    def portfolio_risk(self, portfolio: Portfolio, rows: dict[str, ScreenRow]) -> PortfolioRisk:
        marks = {s: (r.metrics.clean_price, r.quote.accrued, r.bond.face_value) for s, r in rows.items()}
        nav = portfolio.nav(marks)
        dur = dv01 = corp = 0.0
        issuers: dict[str, float] = {}
        for secid, pos in portfolio.positions.items():
            r = rows.get(secid)
            if not r or nav <= 0:
                continue
            val = pos.qty * r.metrics.dirty_price
            w = val / nav
            dur += w * r.metrics.modified_duration
            dv01 += pos.qty * r.metrics.dv01
            if not r.bond.is_ofz:
                corp += w
            issuers[r.bond.issuer_key] = issuers.get(r.bond.issuer_key, 0.0) + w
        sigma = self.limits.yield_vol_bp_daily
        var1 = 1.645 * dv01 * sigma
        var10 = 2.326 * dv01 * sigma * math.sqrt(10)
        return PortfolioRisk(nav, dur, dv01, var1, var10, corp, portfolio.cash / nav if nav > 0 else 1.0, issuers)


def orders_from_targets(portfolio: Portfolio, targets: dict[str, float], rows: dict[str, ScreenRow],
                        min_trade_share: float = 0.005, strategy: str = "", reasons: Optional[dict[str, str]] = None,
                        cash_buffer: float = 0.01) -> list[Order]:
    """Разница между текущими и целевыми весами -> список ордеров (продажи первыми).

    Целевые веса задаются от NAV; на комиссии/НКД оставляется буфер cash_buffer.
    """
    marks = {s: (r.metrics.clean_price, r.quote.accrued, r.bond.face_value) for s, r in rows.items()}
    nav = portfolio.nav(marks)
    if nav <= 0:
        return []
    investable = nav * (1 - cash_buffer)
    orders: list[Order] = []
    reasons = reasons or {}
    all_ids = set(targets) | set(portfolio.positions)
    for secid in sorted(all_ids):
        row = rows.get(secid)
        cur_qty = portfolio.positions[secid].qty if secid in portfolio.positions else 0
        tw = targets.get(secid, 0.0)
        if row is None:
            continue  # нет котировки — ни купить, ни продать
        target_qty = int(math.floor(tw * investable / row.metrics.dirty_price)) if tw > 0 else 0
        delta = target_qty - cur_qty
        if delta == 0:
            continue
        if abs(delta) * row.metrics.dirty_price < min_trade_share * nav and target_qty > 0:
            continue  # слишком маленькая ребалансировка
        side = "BUY" if delta > 0 else "SELL"
        orders.append(Order(secid, side, abs(delta), row.metrics.clean_price, reasons.get(secid, ""), strategy))
    orders.sort(key=lambda o: (o.side != "SELL", o.secid))
    return orders

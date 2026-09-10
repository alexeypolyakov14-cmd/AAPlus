"""Облигационная математика: денежные потоки, цена, YTM, дюрация, выпуклость.

Конвенции соответствуют MOEX: эффективная годовая доходность, ACT/365,
t_i = (дата платежа − дата расчётов) / 365.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Optional

from ..models import Bond, BondMetrics, CashFlow, Quote

DAYS_IN_YEAR = 365.0


# ---------------------------------------------------------------------------
# Денежные потоки
# ---------------------------------------------------------------------------

def build_cash_flows(bond: Bond, settle: date, until: Optional[date] = None,
                     to_offer: bool = False) -> list[CashFlow]:
    """Строит будущие потоки (после даты settle).

    Если у облигации есть полный график из bondization — используется он.
    Иначе график синтезируется из COUPONVALUE / COUPONPERIOD / NEXTCOUPON / MATDATE.
    При to_offer=True потоки обрезаются датой оферты и в неё добавляется
    возврат номинала по цене выкупа.
    """
    end = until
    if to_offer and bond.offer_date:
        end = bond.offer_date
    if end is None:
        end = bond.maturity
    if end is None:
        raise ValueError(f"{bond.secid}: неизвестна дата погашения")

    flows: dict[date, CashFlow] = {}

    def add(d: date, coupon: float = 0.0, principal: float = 0.0):
        prev = flows.get(d)
        if prev:
            flows[d] = CashFlow(d, prev.coupon + coupon, prev.principal + principal)
        else:
            flows[d] = CashFlow(d, coupon, principal)

    if bond.has_full_schedule and bond.coupons:
        last_known = bond.coupon_value or 0.0
        for d, val in bond.coupons:
            if d <= settle or d > end:
                continue
            # Неизвестный (плавающий) купон — считаем равным последнему известному
            v = val if val is not None else last_known
            if val is not None:
                last_known = val
            add(d, coupon=v)
        remaining = bond.face_value
        for d, val in bond.amortizations:
            if d <= settle or d > end:
                continue
            add(d, principal=val)
            remaining -= val
        if to_offer and bond.offer_date and end == bond.offer_date:
            price = (bond.buyback_price or 100.0) / 100.0
            add(end, principal=max(remaining, 0.0) * price)
        elif remaining > 1e-6:
            # график амортизаций мог не содержать финального погашения
            add(end, principal=remaining)
    else:
        if bond.coupon_period and bond.next_coupon:
            cv = bond.coupon_value
            if cv is None and bond.coupon_percent is not None:
                cv = bond.face_value * bond.coupon_percent / 100 * bond.coupon_period / DAYS_IN_YEAR
            cv = cv or 0.0
            d = bond.next_coupon
            step = timedelta(days=bond.coupon_period)
            guard = 0
            while d <= end and guard < 2000:
                if d > settle:
                    add(d, coupon=cv)
                d = d + step
                guard += 1
            # Купон в дату погашения, если график «не дотянулся» до неё
            if end not in flows or flows[end].coupon == 0:
                if d - step < end:
                    frac = (end - (d - step)).days / bond.coupon_period
                    if 0 < frac < 1.0:
                        add(end, coupon=cv * frac)
        if to_offer and bond.offer_date and end == bond.offer_date:
            add(end, principal=bond.face_value * (bond.buyback_price or 100.0) / 100.0)
        else:
            add(end, principal=bond.face_value)

    return [flows[d] for d in sorted(flows)]


def coupon_payments_between(bond: Bond, lo: date, hi: date) -> list[tuple[date, float]]:
    """Купонные выплаты с датой в (lo, hi]. Без полного графика — синтез от next_coupon с шагом coupon_period."""
    if bond.has_full_schedule:
        last = bond.coupon_value or 0.0
        out = []
        for d, v in bond.coupons:
            if v is not None:
                last = v
            if lo < d <= hi and (v if v is not None else last):
                out.append((d, v if v is not None else last))
        return out
    if not (bond.coupon_period and bond.coupon_period > 0 and bond.next_coupon and bond.coupon_value):
        return []
    step = timedelta(days=bond.coupon_period)
    d = bond.next_coupon
    guard = 0
    while d - step > lo and guard < 5000:
        d -= step
        guard += 1
    out = []
    while d <= hi and guard < 10000:
        if d > lo and (bond.maturity is None or d <= bond.maturity):
            out.append((d, bond.coupon_value))
        d += step
        guard += 1
    return out


def accrued_interest(bond: Bond, settle: date) -> float:
    """НКД на дату settle (линейно внутри купонного периода)."""
    if not bond.coupon_period or not bond.next_coupon:
        return 0.0
    cv = bond.coupon_value
    if cv is None and bond.coupon_percent is not None:
        cv = bond.face_value * bond.coupon_percent / 100 * bond.coupon_period / DAYS_IN_YEAR
    if not cv:
        return 0.0
    days_to_next = (bond.next_coupon - settle).days
    if days_to_next < 0:
        return 0.0
    elapsed = bond.coupon_period - days_to_next
    return max(0.0, cv * elapsed / bond.coupon_period)


# ---------------------------------------------------------------------------
# Цена / доходность
# ---------------------------------------------------------------------------

def _t(settle: date, d: date) -> float:
    return (d - settle).days / DAYS_IN_YEAR


def dirty_price_from_yield(flows: Iterable[CashFlow], settle: date, ytm: float) -> float:
    """Грязная цена (руб.) при эффективной доходности ytm (в долях, 0.15 = 15%)."""
    return sum(cf.total / (1.0 + ytm) ** _t(settle, cf.date) for cf in flows)


def ytm_from_dirty_price(flows: list[CashFlow], settle: date, dirty: float,
                         lo: float = -0.95, hi: float = 5.0, tol: float = 1e-10) -> float:
    """Эффективная доходность (в долях) — Ньютон с защитой бисекцией."""
    if dirty <= 0:
        raise ValueError("dirty price must be positive")
    f_lo = dirty_price_from_yield(flows, settle, lo) - dirty
    f_hi = dirty_price_from_yield(flows, settle, hi) - dirty
    if f_lo * f_hi > 0:
        raise ValueError("YTM не локализована в допустимом диапазоне")
    y = 0.1
    for _ in range(100):
        pv = dirty_price_from_yield(flows, settle, y)
        f = pv - dirty
        if abs(f) < tol:
            return y
        # производная dPV/dy
        dpv = -sum(cf.total * _t(settle, cf.date) / (1.0 + y) ** (_t(settle, cf.date) + 1) for cf in flows)
        y_new = y - f / dpv if dpv != 0 else None
        if y_new is None or not (lo < y_new < hi):
            # бисекция
            if f_lo * f < 0:
                hi, f_hi = y, f
            else:
                lo, f_lo = y, f
            y_new = (lo + hi) / 2
        else:
            if f_lo * f < 0:
                hi, f_hi = y, f
            else:
                lo, f_lo = y, f
        y = y_new
    return y


def duration_convexity(flows: list[CashFlow], settle: date, ytm: float) -> tuple[float, float, float]:
    """(Маколей, модифицированная, выпуклость) при эффективной доходности ytm."""
    pv_total = 0.0
    w_t = 0.0
    conv = 0.0
    for cf in flows:
        t = _t(settle, cf.date)
        df = (1.0 + ytm) ** t
        pv = cf.total / df
        pv_total += pv
        w_t += t * pv
        conv += t * (t + 1) * cf.total / (1.0 + ytm) ** (t + 2)
    if pv_total == 0:
        return 0.0, 0.0, 0.0
    mac = w_t / pv_total
    mod = mac / (1.0 + ytm)
    return mac, mod, conv / pv_total


# ---------------------------------------------------------------------------
# Сводные метрики
# ---------------------------------------------------------------------------

def compute_metrics(bond: Bond, quote: Quote, settle: Optional[date] = None,
                    curve=None) -> Optional[BondMetrics]:
    """Полный набор метрик по чистой цене из котировки.

    curve — объект с методом yield_at(years) -> % годовых (для G-спреда), опционально.
    Возвращает None, если нет цены или даты погашения.
    """
    settle = settle or quote.trade_date
    price = quote.price
    if price is None or price <= 0 or bond.maturity is None or bond.maturity <= settle:
        return None
    accrued = quote.accrued if quote.accrued else accrued_interest(bond, settle)
    dirty = bond.face_value * price / 100.0 + accrued

    flows = build_cash_flows(bond, settle)
    try:
        y = ytm_from_dirty_price(flows, settle, dirty)
    except ValueError:
        return None
    mac, mod, conv = duration_convexity(flows, settle, y)

    ytm_offer = None
    if bond.offer_date and settle < bond.offer_date < bond.maturity:
        try:
            of = build_cash_flows(bond, settle, to_offer=True)
            ytm_offer = ytm_from_dirty_price(of, settle, dirty) * 100
        except ValueError:
            ytm_offer = None

    ytm_pct = y * 100
    worst = min(ytm_pct, ytm_offer) if ytm_offer is not None else ytm_pct
    annual_coupon = 0.0
    if bond.coupon_value and bond.coupon_period:
        annual_coupon = bond.coupon_value * DAYS_IN_YEAR / bond.coupon_period
    elif bond.coupon_percent:
        annual_coupon = bond.face_value * bond.coupon_percent / 100
    cur_yield = annual_coupon / (bond.face_value * price / 100.0) * 100 if price else 0.0

    g_spread = None
    if curve is not None:
        try:
            g_spread = (ytm_pct - curve.yield_at(mac)) * 100  # б.п.
        except Exception:
            g_spread = None

    return BondMetrics(
        secid=bond.secid,
        clean_price=price,
        dirty_price=dirty,
        ytm=ytm_pct,
        ytm_to_offer=ytm_offer,
        yield_worst=worst,
        macaulay_duration=mac,
        modified_duration=mod,
        convexity=conv,
        dv01=mod * dirty * 1e-4,
        years_to_maturity=_t(settle, bond.maturity),
        current_yield=cur_yield,
        g_spread=g_spread,
    )


def price_from_ytm(bond: Bond, settle: date, ytm_pct: float) -> float:
    """Чистая цена (% от номинала) при заданной доходности, % годовых."""
    flows = build_cash_flows(bond, settle)
    dirty = dirty_price_from_yield(flows, settle, ytm_pct / 100)
    return (dirty - accrued_interest(bond, settle)) / bond.face_value * 100


def price_change_for_yield_shift(metrics: BondMetrics, shift_bp: float) -> float:
    """Оценка изменения грязной цены (в %) при сдвиге доходности на shift_bp через дюрацию и выпуклость."""
    dy = shift_bp / 10000
    return (-metrics.modified_duration * dy + 0.5 * metrics.convexity * dy * dy) * 100


def rolldown_return(curve, duration_years: float, horizon_years: float = 1.0) -> float:
    """Оценка дохода от «скатывания» по кривой за горизонт, % (приближение через дюрацию)."""
    if duration_years <= horizon_years:
        return 0.0
    y_now = curve.yield_at(duration_years)
    y_later = curve.yield_at(duration_years - horizon_years)
    return (y_now - y_later) / 100 * (duration_years - horizon_years) * 100

"""Сценарный анализ: полная доходность бумаги и корзины за горизонт при параллельном сдвиге доходностей.

Модель намеренно простая и прозрачная:
- график платежей берётся к «худшей» дате (оферта, если она первична в compute_metrics, иначе погашение);
- сдвиг применяется к доходности бумаги (YTW + shift): спред к ОФЗ считается неизменным, кривая — плоской по сдвигу;
- купоны и амортизации внутри горизонта реинвестируются под сдвинутую доходность до конца горизонта;
- остаток потоков за горизонтом дисконтируется на дату горизонта той же сдвинутой доходностью;
- если бумага гасится (или уходит по оферте) внутри горизонта, деньги от погашения тоже реинвестируются до конца горизонта.
Полная доходность = (реинвестированные потоки + стоимость остатка) / грязная цена сегодня − 1, в % за горизонт.
«Мгновенная переоценка» — изменение грязной цены сегодня при том же сдвиге (что покажет брокерский счёт в день шока).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from ..models import Bond, BondMetrics, Quote
from .bond_math import DAYS_IN_YEAR, build_cash_flows, compute_metrics, dirty_price_from_yield


@dataclass
class ScenarioRow:
    secid: str
    name: str
    ytw: float
    duration: float
    dirty_now: float
    horizon_end: date
    matures_in_horizon: bool
    total_return: dict[float, float]     # shift_bp -> % за горизонт
    instant_pnl: dict[float, float]      # shift_bp -> % мгновенной переоценки грязной цены


def _horizon_date(settle: date, horizon_years: float) -> date:
    return settle + timedelta(days=round(horizon_years * DAYS_IN_YEAR))


def scenario_bond(bond: Bond, quote: Quote, settle: date, shifts_bp: list[float], horizon_years: float = 1.0,
                  metrics: Optional[BondMetrics] = None) -> Optional[ScenarioRow]:
    m = metrics or compute_metrics(bond, quote, settle)
    if m is None:
        return None
    offer_primary = bond.offer_date is not None and m.ytm_to_offer is not None and abs(m.yield_worst - m.ytm_to_offer) < 1e-9 \
        and settle < bond.offer_date < (bond.maturity or bond.offer_date)
    flows = build_cash_flows(bond, settle, to_offer=offer_primary)
    end = _horizon_date(settle, horizon_years)
    total: dict[float, float] = {}
    instant: dict[float, float] = {}
    last_flow = max(cf.date for cf in flows)
    for s in shifts_bp:
        y = (m.yield_worst + s / 100) / 100
        # потоки внутри горизонта — реинвестируем до его конца
        reinvested = sum(cf.total * (1.0 + y) ** ((end - cf.date).days / DAYS_IN_YEAR) for cf in flows if cf.date <= end)
        # остаток — оцениваем на дату горизонта
        tail = [cf for cf in flows if cf.date > end]
        value_end = dirty_price_from_yield(tail, end, y) if tail else 0.0
        total[s] = ((reinvested + value_end) / m.dirty_price - 1.0) * 100
        instant[s] = (dirty_price_from_yield(flows, settle, y) / m.dirty_price - 1.0) * 100
    return ScenarioRow(bond.secid, bond.name, m.yield_worst, m.macaulay_duration, m.dirty_price, end,
                       last_flow <= end, total, instant)


def scenario_portfolio(rows: list[ScenarioRow], weights: Optional[dict[str, float]] = None) -> tuple[dict[float, float], dict[float, float], float, float]:
    """Взвешенные полная доходность и мгновенная переоценка корзины; веса по умолчанию равные."""
    if not rows:
        return {}, {}, 0.0, 0.0
    w = weights or {r.secid: 1.0 / len(rows) for r in rows}
    tot = sum(w.get(r.secid, 0.0) for r in rows) or 1.0
    shifts = rows[0].total_return.keys()
    total = {s: sum(w.get(r.secid, 0.0) * r.total_return[s] for r in rows) / tot for s in shifts}
    instant = {s: sum(w.get(r.secid, 0.0) * r.instant_pnl[s] for r in rows) / tot for s in shifts}
    ytw = sum(w.get(r.secid, 0.0) * r.ytw for r in rows) / tot
    dur = sum(w.get(r.secid, 0.0) * r.duration for r in rows) / tot
    return total, instant, ytw, dur

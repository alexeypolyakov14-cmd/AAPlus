"""Мониторинг держащихся позиций: стоп-факторы, просадки, новости, устаревшие заявки.

Скринер проверяет кандидатов на покупку; этот модуль проверяет то, что уже куплено, и формирует алерты:
  critical — бумагу надо продавать / разбираться сегодня (дефолт по реестру MOEX, факт e-disclosure,
             новость СМИ о дефолте/банкротстве, отрицательный капитал, рейтинг ниже допустимого);
  warning  — ухудшение, требующее внимания (падение цены, расширение спреда, негативный новостной фон,
             бумага выпала из скрина по ликвидности, нет котировки);
  info     — активные заявки, ребалансировка.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .data.ratings import rating_at_least
from .portfolio import Portfolio
from .screener import ScreenRow

STOP_MARKERS = ("дефолт", "default", "новости:", "отчётност", "рейтинг", "e-disclosure")


@dataclass
class Alert:
    level: str          # critical | warning | info
    secid: str
    message: str

    def __str__(self) -> str:
        mark = {"critical": "!!", "warning": "!", "info": "~"}.get(self.level, "?")
        return f"[{mark}] {self.secid}: {self.message}" if self.secid else f"[{mark}] {self.message}"


def check_positions(pf: Portfolio, rows_all: dict[str, ScreenRow], rejected: dict[str, str], *,
                    max_g_spread_bp: float = 800, price_drop_pct: float = 5.0, news_alert_score: float = -3.0,
                    min_rating: str = "", open_orders: int = 0) -> list[Alert]:
    """rows_all — строки скрина плюс метрики по держащимся бумагам, не прошедшим скрин (см. cli.cmd_portfolio);
    rejected — причины отсева последнего скрина."""
    out: list[Alert] = []
    for secid, pos in pf.positions.items():
        r = rows_all.get(secid)
        if r is None:
            out.append(Alert("warning", secid, "нет котировки/метрик на MOEX — проверить торги и погашение"))
            continue
        why = rejected.get(secid, "")
        if why:
            level = "critical" if any(m in why.lower() for m in STOP_MARKERS) else "warning"
            out.append(Alert(level, secid, f"выпала из скрина: {why}"))
        if r.stop_events:
            e = r.stop_events[-1]
            out.append(Alert("critical", secid, f"e-disclosure: {e.kind} {e.date} — {e.title[:80]}"))
        if r.fin is not None and r.fin.equity <= 0:
            out.append(Alert("critical", secid, "отчётность: отрицательный капитал"))
        elif r.fin is not None and r.fin.interest_coverage is not None and r.fin.interest_coverage < 1.0:
            out.append(Alert("critical", secid, f"отчётность: покрытие процентов {r.fin.interest_coverage:.1f}x"))
        if min_rating and r.rating is not None and not rating_at_least(r.rating.rating, min_rating):
            out.append(Alert("critical", secid, f"рейтинг {r.rating.rating} ниже допустимого {min_rating}"))
        if r.news is not None and r.news.stop is not None:
            out.append(Alert("critical", secid, f"новости: {r.news.stop.tags.split(',')[0]} {r.news.stop.date} — {r.news.stop.title[:80]}"))
        elif r.news is not None and r.news.n and r.news.score <= news_alert_score:
            worst = f" — {r.news.worst.title[:70]}" if r.news.worst else ""
            out.append(Alert("warning", secid, f"новостной фон {r.news.score:+.1f} ({r.news.negative} негативных){worst}"))
        if pos.avg_price > 0:
            drop = (r.metrics.clean_price / pos.avg_price - 1) * 100
            if drop <= -price_drop_pct:
                out.append(Alert("warning", secid, f"цена {r.metrics.clean_price:.2f} ниже средней покупки {pos.avg_price:.2f} на {-drop:.1f}%"))
        if r.metrics.g_spread is not None and r.metrics.g_spread > max_g_spread_bp:
            out.append(Alert("warning", secid, f"G-спред {r.metrics.g_spread:.0f} б.п. выше лимита {max_g_spread_bp:.0f}"))
        if r.bond.offer_date and r.bond.offer_date > r.quote.trade_date and (r.bond.offer_date - r.quote.trade_date).days <= 14:
            out.append(Alert("info", secid, f"оферта {r.bond.offer_date}: решить — предъявлять или держать (новый купон может быть ниже)"))
    if open_orders:
        out.append(Alert("info", "", f"активных заявок у брокера: {open_orders}"))
    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (order.get(a.level, 3), a.secid))
    return out


def worst_level(alerts: list[Alert]) -> Optional[str]:
    for lvl in ("critical", "warning", "info"):
        if any(a.level == lvl for a in alerts):
            return lvl
    return None

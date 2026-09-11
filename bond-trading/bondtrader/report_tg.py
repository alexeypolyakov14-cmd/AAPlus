"""Рендер ежедневного отчёта для Telegram (HTML parse_mode).

Telegram не рисует Markdown-таблицы, поэтому табличные части — моноширинные <pre>-блоки, свёрстанные под ширину
экрана телефона (~36 символов), а списки — короткие строки с иконками. Ссылки на новости — <a href>.
"""
from __future__ import annotations

import html
from datetime import date
from typing import Iterable

WIDTH_NAME = 12


def _e(s: str) -> str:
    return html.escape(str(s), quote=False)


def _name(s: str, n: int = WIDTH_NAME) -> str:
    s = (s or "").replace(" ", " ")
    return (s[: n - 1] + "…") if len(s) > n else s.ljust(n)


def _money(x: float) -> str:
    """1 234 567 -> '1.23 млн', 45 300 -> '45.3 тыс.'"""
    ax = abs(x)
    if ax >= 1e6:
        return f"{x / 1e6:.2f} млн"
    if ax >= 1e3:
        return f"{x / 1e3:.1f} тыс."
    return f"{x:.0f}"


def _pnl(x: float) -> str:
    sign = "+" if x > 0 else ""
    if abs(x) >= 1e5:
        return f"{sign}{x / 1e3:.0f}к"
    if abs(x) >= 1e3:
        return f"{sign}{x / 1e3:.1f}к"
    return f"{sign}{x:.0f}"


def render_telegram(d: dict) -> str:
    from .monitor import worst_level
    snap, pf, broker, name = d["snap"], d["pf"], d["broker"], d["name"]
    alerts, all_rows, pr, w = d["alerts"], d["all_rows"], d["pr"], d["weights"]
    targets, orders, reasons, by_id, notes = d["targets"], d["orders"], d["reasons"], d["by_id"], d["notes"]
    lvl = worst_level(alerts)
    badge = {"critical": "🔴", "warning": "🟡", "info": "🟢", None: "🟢"}[lvl]
    acct = "песочница" if getattr(broker, "sandbox", False) else ("бумажный" if broker.name == "paper" else "боевой счёт")
    kr = f"{snap.keyrate.current:.1f}%" if snap.keyrate else "н/д"
    cv = f"{snap.curve.yield_at(1):.1f} / {snap.curve.yield_at(3):.1f} / {snap.curve.yield_at(10):.1f}%" if snap.curve else "н/д"

    # --- P&L по позициям (нереализованный, по чистой цене от средней покупки) ---
    unreal = 0.0
    pos_rows = []
    for secid, pos in pf.positions.items():
        r = all_rows.get(secid)
        if r is None:
            pos_rows.append((secid, None, pos, 0.0, 0.0))
            continue
        pnl = pos.qty * pos.face * (r.metrics.clean_price - pos.avg_price) / 100 if pos.avg_price else 0.0
        unreal += pnl
        pos_rows.append((secid, r, pos, w.get(secid, 0.0), pnl))
    pos_rows.sort(key=lambda t: -t[3])

    S: list[str] = []
    S.append(f"{badge} <b>bondtrader · {snap.settle:%d.%m.%Y}</b> · {_e(name)} · {acct}")
    S.append(f"Ключевая {kr} · ОФЗ 1/3/10 лет {cv}")
    S.append("")
    S.append("<b>Портфель</b>")
    S.append(f"NAV <b>{pr.nav:,.0f} ₽</b> · деньги {_money(pf.cash)} ({pr.cash_share:.0%}) · позиций {len(pf.positions)}")
    S.append(f"P&amp;L нереализ. <b>{_pnl(unreal)} ₽</b> · реализ. {_pnl(pf.realized_pnl)} · купоны {_pnl(pf.coupons_received)} · комиссии {pf.commissions_paid:,.0f}")
    S.append(f"Дюрация {pr.duration:.2f} · DV01 {pr.dv01:,.0f} ₽/б.п. · VaR 1д {_money(pr.var_1d_95)} · корпораты {pr.corporate_share:.0%}")
    S.append("")

    # --- алерты ---
    icon = {"critical": "🔴", "warning": "🟡", "info": "ℹ️"}
    S.append(f"<b>Алерты</b> ({len(alerts)})")
    if alerts:
        for a in alerts[:12]:
            who = f"<b>{_e(_label(all_rows, a.secid))}</b>: " if a.secid else ""
            S.append(f"{icon.get(a.level, '•')} {who}{_e(a.message[:140])}")
        if len(alerts) > 12:
            S.append(f"… ещё {len(alerts) - 12}")
    else:
        S.append("✅ всё спокойно")
    S.append("")

    # --- позиции: таблица ---
    if pos_rows:
        S.append(f"<b>Позиции</b> (вес · YTW · цена · P&amp;L)")
        lines = ["Бумага       Вес  YTW  Цена   P&L"]
        for secid, r, pos, wt, pnl in pos_rows:
            if r is None:
                lines.append(f"{_name(secid)} {'—':>4} {'—':>4} {'—':>6} {'—':>6}")
                continue
            lines.append(f"{_name(r.bond.name)}{wt * 100:4.1f} {r.metrics.yield_worst:4.1f} {r.metrics.clean_price:6.2f} {_pnl(pnl):>6}")
        S.append("<pre>" + _e("\n".join(lines)) + "</pre>")
        S.append("")

    # --- цель и ордера ---
    inv = sum(targets.values())
    buys = [o for o in orders if o.side == "BUY"]
    sells = [o for o in orders if o.side == "SELL"]
    S.append(f"<b>Цель {_e(name)}</b>: {len(targets)} бумаг, инвестировано {inv:.0%}")
    hard = [n for n in notes if n.hard]
    soft = [n for n in notes if not n.hard]
    for n in hard[:5]:
        S.append(f"⛔ {_e(n.message[:120])}")
    if soft:
        S.append(f"✂️ лимиты: {_e('; '.join(n.message[:60] for n in soft[:4]))}" + (" …" if len(soft) > 4 else ""))
    if orders:
        S.append(f"Ордера: продать {len(sells)}, купить {len(buys)}")
        lines = ["Бумага       Сторона Кол  Сумма"]
        for o in sorted(orders, key=lambda o: (o.side != "SELL", -(o.qty * (all_rows.get(o.secid).metrics.dirty_price if all_rows.get(o.secid) else 0))))[:12]:
            r = all_rows.get(o.secid) or by_id.get(o.secid)
            amt = o.qty * r.metrics.dirty_price if r else 0.0
            lines.append(f"{_name(r.bond.name if r else o.secid)}{'прод.' if o.side == 'SELL' else 'куп. ':>6} {o.qty:4d} {_money(amt):>9}")
        if len(orders) > 12:
            lines.append(f"… ещё {len(orders) - 12}")
        S.append("<pre>" + _e("\n".join(lines)) + "</pre>")
    else:
        S.append("Ребалансировка не требуется")
    S.append("")

    # --- новости по позициям и целям за 7 дней ---
    if snap.news is not None:
        watch = list(pf.positions) + [s for s in targets if s not in pf.positions]
        items = []
        seen = set()
        for secid in watch:
            r = all_rows.get(secid) or by_id.get(secid)
            if r is None or r.news is None:
                continue
            for it in r.news.items:
                if (snap.settle - it.date).days > 7 or "blog" in it.tags or it.score == 0:
                    continue
                key = it.title.lower()[:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append((it.score, it.date, r.bond.name, it))
        items.sort(key=lambda t: (t[0], -t[1].toordinal()))
        if items:
            S.append("<b>Новости по позициям за 7 дней</b>")
            for sc, dt, nm, it in items[:12]:
                mark = "🔻" if sc <= -3 else ("▫️" if sc < 0 else "🔹")
                title = _e(it.title[:90])
                link = f'<a href="{_e(it.url)}">{title}</a>' if it.url.startswith("http") else title
                S.append(f"{mark} {dt:%d.%m} <b>{_e(nm)}</b> ({sc:+.1f}): {link}")
            if len(items) > 12:
                S.append(f"… ещё {len(items) - 12} в полном отчёте")
        else:
            S.append("<b>Новости</b>: значимых новостей по позициям за 7 дней нет")
        S.append("")

    # --- стоп-факторы скринера (кратко) ---
    stops = [(s, why) for s, why in (snap.rejected or {}).items() if any(m in why.lower() for m in ("дефолт", "default", "новости:"))]
    if stops:
        S.append(f"<b>Отсеяно по стоп-факторам</b>: {len(stops)} бумаг (дефолты MOEX, новости о дефолте/банкротстве)")
    return "\n".join(S).strip()


def _label(all_rows: dict, secid: str) -> str:
    r = all_rows.get(secid)
    return r.bond.name if r is not None else secid

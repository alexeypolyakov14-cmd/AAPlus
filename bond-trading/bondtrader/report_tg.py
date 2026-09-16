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


def _positions(d: dict) -> tuple[list, float]:
    """[(secid, row|None, pos, вес, pnl)], нереализованный P&L по чистой цене от средней покупки."""
    pf, all_rows, w = d["pf"], d["all_rows"], d["weights"]
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
    return pos_rows, unreal


def section_header(d: dict) -> list[str]:
    from .monitor import worst_level
    snap, broker, name, alerts = d["snap"], d["broker"], d["name"], d["alerts"]
    badge = {"critical": "🔴", "warning": "🟡", "info": "🟢", None: "🟢"}[worst_level(alerts)]
    acct = "песочница" if getattr(broker, "sandbox", False) else ("бумажный" if broker.name == "paper" else "боевой счёт")
    kr = f"{snap.keyrate.current:.1f}%" if snap.keyrate else "н/д"
    cv = f"{snap.curve.yield_at(1):.1f} / {snap.curve.yield_at(3):.1f} / {snap.curve.yield_at(10):.1f}%" if snap.curve else "н/д"
    return [f"{badge} <b>bondtrader · {snap.settle:%d.%m.%Y}</b> · {_e(name)} · {acct}", f"Ключевая {kr} · ОФЗ 1/3/10 лет {cv}"]


def section_portfolio(d: dict) -> list[str]:
    pf, pr = d["pf"], d["pr"]
    _, unreal = _positions(d)
    return ["<b>Портфель</b>",
            f"NAV <b>{pr.nav:,.0f} ₽</b> · деньги {_money(pf.cash)} ({pr.cash_share:.0%}) · позиций {len(pf.positions)}",
            f"P&amp;L нереализ. <b>{_pnl(unreal)} ₽</b> · реализ. {_pnl(pf.realized_pnl)} · купоны {_pnl(pf.coupons_received)} · комиссии {pf.commissions_paid:,.0f}",
            f"Дюрация {pr.duration:.2f} · DV01 {pr.dv01:,.0f} ₽/б.п. · VaR 1д {_money(pr.var_1d_95)} · корпораты {pr.corporate_share:.0%}"]


def section_alerts(d: dict, limit: int = 12) -> list[str]:
    alerts, all_rows = d["alerts"], d["all_rows"]
    icon = {"critical": "🔴", "warning": "🟡", "info": "ℹ️"}
    S = [f"<b>Алерты</b> ({len(alerts)})"]
    if not alerts:
        return S + ["✅ всё спокойно"]
    for a in alerts[:limit]:
        who = f"<b>{_e(_label(all_rows, a.secid))}</b>: " if a.secid else ""
        S.append(f"{icon.get(a.level, '•')} {who}{_e(a.message[:140])}")
    if len(alerts) > limit:
        S.append(f"… ещё {len(alerts) - limit}")
    return S


def section_positions(d: dict) -> list[str]:
    pos_rows, unreal = _positions(d)
    if not pos_rows:
        return ["<b>Позиции</b>: нет"]
    lines = ["Бумага       Вес  YTW  Цена   P&L"]
    for secid, r, pos, wt, pnl in pos_rows:
        if r is None:
            lines.append(f"{_name(secid)} {'—':>4} {'—':>4} {'—':>6} {'—':>6}")
            continue
        lines.append(f"{_name(r.bond.name)}{wt * 100:4.1f} {r.metrics.yield_worst:4.1f} {r.metrics.clean_price:6.2f} {_pnl(pnl):>6}")
    lines.append(f"{'Итого P&L':<30}{_pnl(unreal):>6}")
    return [f"<b>Позиции</b> ({len(pos_rows)}; вес % · YTW % · цена · P&amp;L ₽)", "<pre>" + _e("\n".join(lines)) + "</pre>"]


def section_target(d: dict) -> list[str]:
    name, targets, orders, all_rows, by_id, notes = d["name"], d["targets"], d["orders"], d["all_rows"], d["by_id"], d["notes"]
    S: list[str] = []
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
    return S


def section_news(d: dict, days: int = 7, limit: int = 12) -> list[str]:
    snap, pf, targets, all_rows, by_id = d["snap"], d["pf"], d["targets"], d["all_rows"], d["by_id"]
    S: list[str] = []
    if snap.news is not None:
        watch = list(pf.positions) + [s for s in targets if s not in pf.positions]
        items = []
        seen = set()
        for secid in watch:
            r = all_rows.get(secid) or by_id.get(secid)
            if r is None or r.news is None:
                continue
            for it in r.news.items:
                if (snap.settle - it.date).days > days or "blog" in it.tags or it.score == 0:
                    continue
                key = it.title.lower()[:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append((it.score, it.date, r.bond.name, it))
        items.sort(key=lambda t: (t[0], -t[1].toordinal()))
        if items:
            S.append(f"<b>Новости по позициям за {days} дней</b>")
            for sc, dt, nm, it in items[:limit]:
                mark = "🔻" if sc <= -3 else ("▫️" if sc < 0 else "🔹")
                title = _e(it.title[:90])
                link = f'<a href="{_e(it.url)}">{title}</a>' if it.url.startswith("http") else title
                S.append(f"{mark} {dt:%d.%m} <b>{_e(nm)}</b> ({sc:+.1f}): {link}")
            if len(items) > limit:
                S.append(f"… ещё {len(items) - limit}")
        else:
            S.append(f"<b>Новости</b>: значимых новостей по позициям за {days} дней нет")
    else:
        S.append("<b>Новости</b>: книга новостей пуста")
    return S


def section_basket(d: dict, flags_limit: int = 10) -> list[str]:
    """Корзина (накопительная книга): живой срез по спискам A/B, флаги, пауза и лист ожидания."""
    from .basket import STATUS_RU
    book, basket = d.get("basket_book"), d.get("basket") or []
    if book is None or not book.live():
        return []
    S: list[str] = [f"<b>Корзина</b> · {_e(book.summary())}"]
    for lst in ("A", "B"):
        rows = [br for br in basket if br.entry.list == lst and br.entry.status == "active"]
        if not rows:
            continue
        lines = ["Бумага      Вес  YTW Спред  Пиры"]
        wsum = ysum = dsum = 0.0
        for br in sorted(rows, key=lambda x: -x.entry.weight):
            e, r = br.entry, br.row
            mark = "!" if br.flags else " "
            if r is None:
                lines.append(f"{_name(e.name, 11)}{e.weight:4.0f}    —     —     —{mark}")
                continue
            gs = r.metrics.g_spread
            px = f"{br.peers_excess:+5.0f}" if br.peers_excess is not None else "    —"
            lines.append(f"{_name(r.bond.name, 11)}{e.weight:4.0f} {r.metrics.yield_worst:4.1f} {gs if gs is None else round(gs):5} {px}{mark}")
            wsum += e.weight
            ysum += e.weight * r.metrics.yield_worst
            dsum += e.weight * r.metrics.macaulay_duration
        if wsum:
            lines.append(f"{'Список ' + lst + ' (вес ' + f'{wsum:.0f}%)':<16}{ysum / wsum:4.1f}%  дюр. {dsum / wsum:.1f}")
        # <pre>-таблица отдельным блоком (пустые строки вокруг): нарезка сообщений идёт по блокам и не рвёт таблицу
        S += ["", "<pre>" + _e("\n".join(lines)) + "</pre>", ""]
    flagged = [(br, f) for br in basket if br.entry.status == "active" for f in br.flags]
    for br, f in flagged[:flags_limit]:
        S.append(f"⚠️ <b>{_e(br.name)}</b>: {_e(f)}")
    if len(flagged) > flags_limit:
        S.append(f"… ещё {len(flagged) - flags_limit}")
    parked = [br for br in basket if br.entry.status in ("hold", "wait")]
    if parked:
        S.append("Вне портфеля: " + "; ".join(f"{_e(br.name)} ({STATUS_RU[br.entry.status]}"
                                              + (f", YTW {br.row.metrics.yield_worst:.1f}%" if br.row is not None else "") + ")" for br in parked))
    S.append("Пиры — премия к медиане похожих, б.п.; ! — есть флаг. Состав меняется только командой basket.")
    return S


def section_stops(d: dict) -> list[str]:
    snap = d["snap"]
    stops = [(s, why) for s, why in (snap.rejected or {}).items() if any(m in why.lower() for m in ("дефолт", "default", "новости:"))]
    return [f"<b>Отсеяно по стоп-факторам</b>: {len(stops)} бумаг (дефолты MOEX, новости о дефолте/банкротстве)"] if stops else []


def section_screen(d: dict, limit: int = 15) -> list[str]:
    """Топ кандидатов value_hy: композит и его разложение — «за что платят больше и почему»."""
    targets, by_id, reasons, name = d["targets"], d["by_id"], d["reasons"], d["name"]
    if not targets:
        return [f"<b>Скрин {_e(name)}</b>: кандидатов нет"]
    lines = ["Бумага      Рейт  YTW Спред Комп"]
    picks = sorted(targets.items(), key=lambda kv: -kv[1])
    for secid, wt in picks[:limit]:
        r = by_id.get(secid)
        if r is None or r.bond.is_ofz:
            continue
        rating = r.rating.rating if r.rating else "—"
        comp = ""
        why = reasons.get(secid, "")
        if "композит" in why:
            try:
                comp = why.split("композит")[1].split(":")[0].strip()
            except IndexError:
                comp = ""
        gs = r.metrics.g_spread or 0
        lines.append(f"{_name(r.bond.name, 11)} {rating:>4} {r.metrics.yield_worst:4.1f} {gs:5.0f} {comp:>5}")
    return [f"<b>Скрин {_e(name)}</b>: {len(targets)} бумаг в цели, инвестировано {sum(targets.values()):.0%}",
            "<pre>" + _e("\n".join(lines)) + "</pre>",
            "Комп — композит: остаток спреда к справедливому + отчётность vs рейтинг + доходность за вычетом PD·LGD + новости."]


def render_telegram(d: dict) -> str:
    parts = [section_header(d), section_portfolio(d), section_alerts(d), section_basket(d), section_positions(d), section_target(d),
             section_news(d), section_stops(d)]
    return "\n\n".join("\n".join(p) for p in parts if p).strip()


def _label(all_rows: dict, secid: str) -> str:
    r = all_rows.get(secid)
    return r.bond.name if r is not None else secid

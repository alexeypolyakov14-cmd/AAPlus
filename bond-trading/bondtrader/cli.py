"""Командная строка bondtrader."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from . import __version__
from .config import Settings
from .market import MarketSnapshot, load_snapshot
from .portfolio import Portfolio
from .risk import RiskLimits, RiskManager, orders_from_targets
from .screener import Screener, ScreenerConfig, ScreenRow, to_dataframe
from .strategies import STRATEGIES, MarketContext, make_strategy

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_rows", 500)

SCREEN_COLS = ["secid", "name", "level", "rating", "price", "ytm", "ytm_offer", "yield_worst", "duration", "g_spread", "cur_yield",
               "turnover_mln", "maturity", "score", "flags"]


def _parse_params(items: Optional[list[str]]) -> dict:
    out: dict = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--param ожидает key=value, получено: {it}")
        k, v = it.split("=", 1)
        v = v.strip()
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.startswith("["):
            out[k] = [float(x) for x in v.strip("[]").split(",") if x.strip()]
        else:
            try:
                out[k] = int(v) if v.lstrip("-").isdigit() else float(v)
            except ValueError:
                out[k] = v
    return out


def _strategy_spec(args, settings) -> tuple[str, dict]:
    """Имя стратегии и параметры: из конфига берём params только для стратегии с тем же именем."""
    cfg_name = settings.get("strategy", "name", default="carry")
    name = getattr(args, "strategy", None) or cfg_name
    params = dict(settings.get("strategy", "params", default={}) or {}) if name == cfg_name else {}
    params.update(_parse_params(getattr(args, "param", None)))
    return name, params


def _screen(snap: MarketSnapshot, settings: Settings, args) -> list[ScreenRow]:
    cfg = ScreenerConfig.from_dict(settings.get("screener", default={}))
    if getattr(args, "ofz_only", False):
        cfg.ofz_only = True
    if getattr(args, "corporate_only", False):
        cfg.corporate_only = True
    if getattr(args, "include_floaters", False):
        cfg.exclude_floaters = False
    if getattr(args, "min_turnover", None) is not None:
        cfg.min_turnover = args.min_turnover
    if getattr(args, "max_duration", None) is not None:
        cfg.max_duration = args.max_duration
    if getattr(args, "min_rating", None):
        cfg.min_rating = args.min_rating
    scr = Screener(cfg)
    rows = scr.run(snap.universe, snap.curve, snap.settle, **snap.screen_kwargs())
    snap.rejected = dict(scr.rejected)
    if getattr(args, "verbose", False):
        rej = pd.Series(scr.rejected).value_counts()
        print("Отсев по причинам:\n" + rej.to_string(), file=sys.stderr)
    return rows


def _print_df(df: pd.DataFrame, cols: Optional[list[str]] = None, csv: Optional[str] = None) -> None:
    if df.empty:
        print("(пусто)")
        return
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    if csv:
        df.to_csv(csv, index=False)
        print(f"сохранено: {csv}", file=sys.stderr)
    print(df.to_string(index=False))


def _header(snap: MarketSnapshot) -> None:
    kr = f"{snap.keyrate.current:.2f}% ({snap.keyrate.regime})" if snap.keyrate else "н/д"
    cv = f"{snap.curve.source}, 1Y {snap.curve.yield_at(1):.2f}% / 10Y {snap.curve.yield_at(10):.2f}%" if snap.curve else "н/д"
    rt = f"{len(snap.ratings)} записей" if snap.ratings else "нет"
    fin = f"{snap.financials.issuers} эмитентов" if snap.financials else "нет"
    ev = f"{len(snap.events)} событий" if snap.events else "нет"
    nw = f"{len(snap.news)} новостей" if snap.news else "нет"
    print(f"Дата: {snap.settle}  Источник: {snap.source}  Бумаг: {len(snap.universe)}  Ключевая ставка: {kr}  Кривая: {cv}  "
          f"Рейтинги: {rt}  Отчётность: {fin}  e-disclosure: {ev}  Новости: {nw}\n")


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def cmd_screen(args, settings):
    snap = load_snapshot(settings, args.fixtures)
    _header(snap)
    rows = _screen(snap, settings, args)
    df = to_dataframe(rows).head(args.top)
    _print_df(df, SCREEN_COLS, args.csv)


def cmd_curve(args, settings):
    snap = load_snapshot(settings, args.fixtures)
    _header(snap)
    if not snap.curve:
        print("кривая недоступна")
        return
    df = pd.DataFrame(snap.curve.points, columns=["tenor_years", "yield_pct"])
    print(df.to_string(index=False))
    print(f"\nНаклон 10Y−1Y: {snap.curve.slope(1, 10):+.2f} п.п.; 2Y−1Y: {snap.curve.slope(1, 2):+.2f} п.п.")


def cmd_keyrate(args, settings):
    snap = load_snapshot(settings, args.fixtures)
    if not snap.keyrate:
        print("ключевая ставка недоступна")
        return
    k = snap.keyrate
    print(f"Ключевая ставка: {k.current:.2f}%  режим: {k.regime}  последнее изменение: {k.last_change:+.2f} п.п. "
          f"от {k.last_change_date}  подряд решений в ту же сторону: {k.consecutive_moves}  макс/мин окна: {k.peak}/{k.trough}")
    changes = [(d, r) for i, (d, r) in enumerate(snap.keyrate_history) if i == 0 or r != snap.keyrate_history[i - 1][1]]
    print(pd.DataFrame(changes[-15:], columns=["date", "rate"]).to_string(index=False))


def cmd_bond(args, settings):
    snap = load_snapshot(settings, args.fixtures)
    from .analytics.bond_math import compute_metrics
    pair = next(((b, q) for b, q in snap.universe if b.secid == args.secid.upper() or b.isin == args.secid.upper()), None)
    if not pair:
        raise SystemExit(f"{args.secid}: не найдена на площадках {settings.get('data', 'boards')}")
    bond, q = pair
    if snap.enrich:
        bond = snap.enrich(bond)
    m = compute_metrics(bond, q, snap.settle, snap.curve)
    print(f"{bond.secid} {bond.name}  ISIN {bond.isin}  площадка {bond.board}  листинг {bond.list_level}")
    print(f"Номинал {bond.face_value:.2f} {bond.currency}  купон {bond.coupon_percent}% ({bond.coupon_value} руб. / {bond.coupon_period} дн.)  "
          f"след. купон {bond.next_coupon}  погашение {bond.maturity}  оферта {bond.offer_date or '—'}")
    print(f"Флоатер: {bond.is_floater}  линкер: {bond.is_linker}  амортизация: {bond.has_amortization}  график загружен: {bond.has_full_schedule}")
    print(f"Цена {q.price}  bid/ask {q.bid}/{q.ask}  НКД {q.accrued}  оборот {q.turnover/1e6:.1f} млн  YTM MOEX {q.ytm_moex}  дюрация MOEX {q.duration_moex}")
    if m:
        print(f"\nРасчёт: YTM {m.ytm:.2f}%  к оферте {m.ytm_to_offer and round(m.ytm_to_offer, 2)}  YTW {m.yield_worst:.2f}%  "
              f"дюрация Маколея {m.macaulay_duration:.2f}  мод. {m.modified_duration:.2f}  выпуклость {m.convexity:.1f}  "
              f"DV01 {m.dv01:.3f} руб.  тек. дох. {m.current_yield:.2f}%  G-спред {m.g_spread and round(m.g_spread)} б.п.")
    if bond.has_full_schedule and args.schedule:
        from .analytics.bond_math import build_cash_flows
        flows = build_cash_flows(bond, snap.settle)
        print("\nДенежные потоки:")
        print(pd.DataFrame([(f.date, f.coupon, f.principal) for f in flows], columns=["date", "coupon", "principal"]).to_string(index=False))


def _build_context(args, settings) -> tuple[MarketSnapshot, list[ScreenRow], Portfolio, object]:
    snap = load_snapshot(settings, args.fixtures)
    rows = RiskManager(RiskLimits.from_dict(settings.get("risk", default={}))).eligible(_screen(snap, settings, args))
    broker = _make_broker(args, settings)
    pf = _broker_portfolio(broker, rows, settings)
    return snap, rows, pf, broker


def _make_broker(args, settings):
    name = getattr(args, "broker", None) or settings.get("execution", "broker", default="paper")
    if name == "paper":
        from .execution.paper import PaperBroker
        return PaperBroker(settings.get("execution", "state_path", default="state/portfolio.json"),
                           initial_cash=settings.get("backtest", "initial_cash", default=1_000_000),
                           commission_bp=settings.get("backtest", "commission_bp", default=5))
    if name == "tinvest":
        from .execution.tinvest import TInvestBroker
        t = settings.get("execution", "tinvest", default={})
        sandbox = t.get("sandbox", True) and not getattr(args, "live", False)
        return TInvestBroker(settings.tinvest_token, sandbox=sandbox, account_id=t.get("account_id", ""), ca_bundle=t.get("ca_bundle") or None,
                             order_type=t.get("order_type", "auto"))
    raise SystemExit(f"неизвестный брокер {name}")


def _broker_portfolio(broker, rows: list[ScreenRow], settings) -> Portfolio:
    if broker.name == "paper":
        return broker.portfolio
    pf = Portfolio(cash=broker.cash())
    by_id = {r.secid: r for r in rows}
    detailed = broker.positions_detailed() if hasattr(broker, "positions_detailed") else {s: (q, None) for s, q in broker.positions().items()}
    from .portfolio import Position
    for secid, (qty, avg) in detailed.items():
        price = by_id[secid].metrics.clean_price if secid in by_id else 100.0
        face = by_id[secid].bond.face_value if secid in by_id else 1000.0
        pf.positions[secid] = Position(secid, qty, avg if avg else price, face)
    return pf


def cmd_signals(args, settings):
    snap, rows, pf, _ = _build_context(args, settings)
    _header(snap)
    name, params = _strategy_spec(args, settings)
    st = make_strategy(name, params)
    ctx = MarketContext(snap.settle, rows, snap.curve, snap.keyrate, pf)
    by_id = ctx.by_id
    targets = st.targets(ctx)
    risk = RiskManager(RiskLimits.from_dict(settings.get("risk", default={})))
    targets, notes = risk.enforce_targets(targets, by_id)
    reasons = st.explain(ctx)
    all_rows = _rows_with_positions(snap, rows, pf)
    print(f"Стратегия: {name} {params or ''}\n")
    df = pd.DataFrame([{"secid": s, "name": by_id[s].bond.name, "weight": round(w * 100, 2), "ytw": round(by_id[s].metrics.yield_worst, 2),
                        "duration": round(by_id[s].metrics.macaulay_duration, 2), "g_spread": by_id[s].metrics.g_spread and round(by_id[s].metrics.g_spread),
                        "reason": reasons.get(s, "")} for s, w in sorted(targets.items(), key=lambda kv: -kv[1])])
    _print_df(df, csv=args.csv)
    if targets:
        tw = sum(targets.values())
        dur = sum(w * by_id[s].metrics.modified_duration for s, w in targets.items()) / tw
        ytw = sum(w * by_id[s].metrics.yield_worst for s, w in targets.items()) / tw
        print(f"\nЦелевой портфель: инвестировано {tw:.1%} (остальное — деньги), мод. дюрация бумаг {dur:.2f}, YTW {ytw:.2f}%")
    for n in notes:
        print(f"  [{'!' if n.hard else '~'}] {n.message}")
    orders = orders_from_targets(pf, targets, all_rows, strategy=name, reasons=reasons)
    if orders:
        print("\nОрдера относительно текущего портфеля:")
        print(pd.DataFrame([o.as_dict() for o in orders]).to_string(index=False))
    else:
        print("\nРебалансировка не требуется.")


def cmd_trade(args, settings):
    from .execution.executor import Executor
    snap, rows, pf, broker = _build_context(args, settings)
    _header(snap)
    name, params = _strategy_spec(args, settings)
    st = make_strategy(name, params)
    ctx = MarketContext(snap.settle, rows, snap.curve, snap.keyrate, pf)
    by_id = ctx.by_id
    risk = RiskManager(RiskLimits.from_dict(settings.get("risk", default={})))
    targets, notes = risk.enforce_targets(st.targets(ctx), by_id)
    # позиции, выпавшие из скрина (структурные, дефолт, ликвидность), тоже надо уметь продать
    by_id = _rows_with_positions(snap, rows, pf)
    orders = orders_from_targets(pf, targets, by_id, strategy=name, reasons=st.explain(ctx))
    for n in notes:
        print(f"  [{'!' if n.hard else '~'}] {n.message}")
    if not orders:
        print("Ребалансировка не требуется.")
        return
    print(pd.DataFrame([o.as_dict() for o in orders]).to_string(index=False))
    dry = not args.confirm
    if broker.name == "tinvest" and not broker.sandbox and not dry:
        print("\n!!! БОЕВОЙ СЧЁТ T-Invest. Реальные деньги.")
        if input("Введите YES для подтверждения: ").strip() != "YES":
            print("отменено")
            return
    ex = Executor(broker, risk, settings.get("execution", "journal_path", default="state/orders.jsonl"), dry_run=dry,
                  cancel_open=not getattr(args, "keep_orders", False))
    rep = ex.run(orders, by_id, pf)
    if rep.aborted:
        print("\nИсполнение остановлено риск-контролем:")
        for v in rep.violations:
            print(f"  [{'!' if v.hard else '~'}] {v.message}")
        return
    print(f"\nРежим: {'DRY-RUN (ничего не отправлено; добавьте --confirm)' if dry else broker.name}"
          + (f"; снято старых заявок: {rep.cancelled}" if rep.cancelled else ""))
    print(pd.DataFrame([{"secid": r.order.secid, "side": r.order.side, "qty": r.order.qty, "status": r.status,
                         "fill_price": r.fill_price, "commission": round(r.commission, 2), "msg": r.message} for r in rep.reports]).to_string(index=False))


def cmd_portfolio(args, settings):
    snap, rows, pf, broker = _build_context(args, settings)
    _header(snap)
    by_id = {r.secid: r for r in rows}
    # для оценки нужны и бумаги, не прошедшие скрин
    from .analytics.bond_math import compute_metrics
    for b, q in snap.universe:
        if b.secid in pf.positions and b.secid not in by_id:
            m = compute_metrics(b, q, snap.settle, snap.curve)
            if m:
                by_id[b.secid] = ScreenRow(b, q, m)
    risk = RiskManager(RiskLimits.from_dict(settings.get("risk", default={})))
    pr = risk.portfolio_risk(pf, by_id)
    print(f"Брокер: {broker.name}  счёт: {broker.account_id()}")
    print(f"NAV {pr.nav:,.0f} руб.  деньги {pf.cash:,.0f} ({pr.cash_share:.1%})  мод. дюрация {pr.duration:.2f}  DV01 {pr.dv01:,.0f} руб./б.п.  "
          f"VaR 1д 95% {pr.var_1d_95:,.0f}  VaR 10д 99% {pr.var_10d_99:,.0f}  корпораты {pr.corporate_share:.1%}")
    if pf.positions:
        recs = []
        for s, p in pf.positions.items():
            r = by_id.get(s)
            val = p.qty * r.metrics.dirty_price if r else p.cost()
            recs.append({"secid": s, "name": r.bond.name if r else "?", "qty": p.qty, "avg_price": round(p.avg_price, 2),
                         "price": r.metrics.clean_price if r else None, "value": round(val), "weight": round(val / pr.nav * 100, 2) if pr.nav else 0,
                         "ytw": r and round(r.metrics.yield_worst, 2), "duration": r and round(r.metrics.macaulay_duration, 2),
                         "pnl": round(p.qty * p.face * ((r.metrics.clean_price if r else p.avg_price) - p.avg_price) / 100)})
        print(pd.DataFrame(recs).to_string(index=False))
    if pr.issuer_exposure:
        print("\nЭкспозиция по эмитентам: " + ", ".join(f"{k} {v:.1%}" for k, v in sorted(pr.issuer_exposure.items(), key=lambda kv: -kv[1])))
    try:
        open_orders = broker.open_orders()
    except Exception as e:  # noqa: BLE001
        open_orders = []
        print(f"\nАктивные заявки: не удалось получить ({e})")
    if open_orders:
        from .execution.tinvest import from_quotation, g as _g
        recs = [{"order_id": _g(o, "order_id", "orderId"), "instrument": _g(o, "instrument_uid", "instrumentUid", "figi"),
                 "direction": str(_g(o, "direction", default="")).replace("ORDER_DIRECTION_", ""),
                 "lots": _g(o, "lots_requested", "lotsRequested"), "executed": _g(o, "lots_executed", "lotsExecuted"),
                 "price": from_quotation(_g(o, "initial_security_price", "initialSecurityPrice")),
                 "status": str(_g(o, "execution_report_status", "executionReportStatus", default="")).replace("EXECUTION_REPORT_STATUS_", "")}
                for o in open_orders]
        print(f"\nАктивные заявки: {len(open_orders)} (деньги под ними заблокированы и не входят в «деньги» выше)")
        print(pd.DataFrame(recs).to_string(index=False))
    print(f"\nРеализованный PnL {pf.realized_pnl:,.0f}  купоны {pf.coupons_received:,.0f}  комиссии {pf.commissions_paid:,.0f}")


def _rows_with_positions(snap: MarketSnapshot, rows: list[ScreenRow], pf: Portfolio) -> dict[str, ScreenRow]:
    """Строки скрина + метрики по держащимся бумагам, которые скрин не прошли (для оценки и мониторинга)."""
    from .analytics.bond_math import compute_metrics
    by_id = {r.secid: r for r in rows}
    for b, q in snap.universe:
        if b.secid in pf.positions and b.secid not in by_id:
            bb = snap.enrich(b) if snap.enrich is not None and not b.has_full_schedule else b
            m = compute_metrics(bb, q, snap.settle, snap.curve)
            if m:
                rating = snap.ratings.lookup(bb) if snap.ratings is not None and not bb.is_ofz else None
                issuer = snap.issuers.lookup(bb) if snap.issuers is not None and not bb.is_ofz else None
                inn = issuer.inn if issuer else ""
                row = ScreenRow(bb, q, m, rating=rating, inn=inn)
                if snap.financials is not None and inn:
                    row.fin = snap.financials.metrics(inn)
                if snap.events is not None and not bb.is_ofz:
                    row.stop_events = snap.events.stop_factors(snap.settle, 365, inn=inn, name=bb.full_name or bb.name)
                if snap.news is not None and not bb.is_ofz:
                    row.news = snap.news.issuer_score(snap.settle, name=bb.full_name or bb.name, inn=inn)
                by_id[b.secid] = row
    return by_id


def _alerts(snap: MarketSnapshot, rows: list[ScreenRow], pf: Portfolio, broker, settings):
    from .monitor import check_positions
    by_id = _rows_with_positions(snap, rows, pf)
    try:
        open_orders = len(broker.open_orders())
    except Exception:  # noqa: BLE001
        open_orders = 0
    mon = settings.get("monitor", default={}) or {}
    return check_positions(pf, by_id, snap.rejected,
                           max_g_spread_bp=settings.get("risk", "max_g_spread_bp", default=800),
                           price_drop_pct=mon.get("price_drop_pct", 5.0), news_alert_score=mon.get("news_alert_score", -3.0),
                           min_rating=mon.get("min_rating") or settings.get("risk", "min_rating", default=""),
                           open_orders=open_orders), by_id


def cmd_monitor(args, settings):
    from .monitor import worst_level
    snap, rows, pf, broker = _build_context(args, settings)
    _header(snap)
    alerts, _ = _alerts(snap, rows, pf, broker, settings)
    print(f"Брокер: {broker.name}  позиций: {len(pf.positions)}  алертов: {len(alerts)}")
    for a in alerts:
        print("  " + str(a))
    if not alerts:
        print("  всё спокойно")
    lvl = worst_level(alerts)
    if args.strict and lvl == "critical":
        return 2
    return 0


def build_report(args, settings) -> str:
    """Ежедневный отчёт в Markdown: рынок, портфель, алерты, целевой портфель и ордера, негатив по эмитентам."""
    from .monitor import worst_level
    snap, rows, pf, broker = _build_context(args, settings)
    name, params = _strategy_spec(args, settings)
    st = make_strategy(name, params)
    ctx = MarketContext(snap.settle, rows, snap.curve, snap.keyrate, pf)
    by_id = ctx.by_id
    targets, notes = RiskManager(RiskLimits.from_dict(settings.get("risk", default={}))).enforce_targets(st.targets(ctx), by_id)
    reasons = st.explain(ctx)
    orders = orders_from_targets(pf, targets, by_id, strategy=name, reasons=reasons)
    alerts, all_rows = _alerts(snap, rows, pf, broker, settings)
    risk = RiskManager(RiskLimits.from_dict(settings.get("risk", default={})))
    pr = risk.portfolio_risk(pf, all_rows)
    kr = f"{snap.keyrate.current:.2f}% ({snap.keyrate.regime})" if snap.keyrate else "н/д"
    cv = f"1Y {snap.curve.yield_at(1):.2f}% / 3Y {snap.curve.yield_at(3):.2f}% / 10Y {snap.curve.yield_at(10):.2f}%" if snap.curve else "н/д"
    L = []
    lvl = worst_level(alerts)
    badge = {"critical": "🔴", "warning": "🟡", "info": "🟢", None: "🟢"}[lvl]
    L.append(f"# {badge} bondtrader {snap.settle} — {name} ({broker.name}{', песочница' if getattr(broker, 'sandbox', False) else ''})")
    L.append("")
    L.append(f"**Рынок.** Ключевая ставка {kr}; кривая ОФЗ {cv}; в скрине {len(rows)} бумаг из {len(snap.universe)}; "
             f"рейтинги {len(snap.ratings) if snap.ratings else 0}, отчётность {snap.financials.issuers if snap.financials else 0} эмит., "
             f"новости {len(snap.news) if snap.news else 0}.")
    L.append("")
    L.append(f"**Портфель.** NAV {pr.nav:,.0f} руб., деньги {pf.cash:,.0f} ({pr.cash_share:.0%}), позиций {len(pf.positions)}, "
             f"мод. дюрация {pr.duration:.2f}, DV01 {pr.dv01:,.0f} руб./б.п., VaR 1д 95% {pr.var_1d_95:,.0f}, корпораты {pr.corporate_share:.0%}. "
             f"Реализовано {pf.realized_pnl:,.0f}, купоны {pf.coupons_received:,.0f}, комиссии {pf.commissions_paid:,.0f}.")
    L.append("")
    L.append(f"## Алерты ({len(alerts)})")
    L += [f"- {a}" for a in alerts] or ["- всё спокойно"]
    L.append("")
    if pf.positions:
        L.append("## Позиции")
        L.append("| secid | бумага | qty | ср. цена | цена | вес | YTW | дюр. | рейтинг | новости |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---|---:|")
        marks = {s: (r.metrics.clean_price, r.quote.accrued, r.bond.face_value) for s, r in all_rows.items()}
        w = pf.weights(marks)
        for secid, pos in sorted(pf.positions.items(), key=lambda kv: -w.get(kv[0], 0)):
            r = all_rows.get(secid)
            if r is None:
                L.append(f"| {secid} | ? | {pos.qty} | {pos.avg_price:.2f} | — | — | — | — | — | — |")
                continue
            nw = f"{r.news.score:+.1f}" if r.news is not None and r.news.n else "—"
            L.append(f"| {secid} | {r.bond.name} | {pos.qty} | {pos.avg_price:.2f} | {r.metrics.clean_price:.2f} | {w.get(secid, 0):.1%} | "
                     f"{r.metrics.yield_worst:.1f}% | {r.metrics.macaulay_duration:.1f} | {r.rating_str} | {nw} |")
        L.append("")
    L.append(f"## Целевой портфель {name}: {len(targets)} бумаг, инвестировано {sum(targets.values()):.0%}")
    for n in notes:
        L.append(f"- [{'!' if n.hard else '~'}] {n.message}")
    if orders:
        L.append("")
        L.append(f"**Ордера для ребалансировки ({len(orders)}):**")
        L.append("| secid | бумага | side | qty | цена | сумма | почему |")
        L.append("|---|---|---|---:|---:|---:|---|")
        for o in orders:
            r = by_id.get(o.secid)
            amt = o.qty * r.metrics.dirty_price if r else 0
            L.append(f"| {o.secid} | {r.bond.name if r else '?'} | {o.side} | {o.qty} | {o.price:.2f} | {amt:,.0f} | {(reasons.get(o.secid, '') or '')[:90]} |")
    else:
        L.append("- ребалансировка не требуется")
    L.append("")
    if snap.news is not None:
        watch = set(pf.positions) | set(targets)
        neg = []
        for secid in watch:
            r = all_rows.get(secid) or by_id.get(secid)
            if r is None or r.news is None:
                continue
            for it in r.news.items[:20]:
                if it.score < 0 and (snap.settle - it.date).days <= 7:
                    neg.append((it.date, r.bond.name, it.score, it.title))
        if neg:
            L.append("## Негативные новости за 7 дней по позициям и целям")
            for d, nm, sc, title in sorted(set(neg), reverse=True)[:25]:
                L.append(f"- {d} {nm} ({sc:+.1f}): {title[:110]}")
            L.append("")
    if snap.rejected:
        stops = [(s, why) for s, why in snap.rejected.items() if any(m in why.lower() for m in ("дефолт", "default", "новости:"))]
        if stops:
            L.append(f"## Отсев по стоп-факторам ({len(stops)})")
            L += [f"- {s}: {why}" for s, why in stops[:30]]
            L.append("")
    return "\n".join(L)


def cmd_report(args, settings):
    md = build_report(args, settings)
    if args.md:
        os.makedirs(os.path.dirname(args.md) or ".", exist_ok=True)
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"отчёт сохранён: {args.md}", file=sys.stderr)
    print(md)
    if args.telegram:
        from .notify import strip_markdown, telegram_send
        n = telegram_send(strip_markdown(md))
        print(f"отправлено в Telegram: {n} сообщ.", file=sys.stderr)


def cmd_notify(args, settings):
    from .notify import strip_markdown, telegram_send, telegram_whoami
    if args.whoami:
        bot, chats = telegram_whoami()
        print(f"Токен принадлежит боту @{bot} (https://t.me/{bot}).")
        if not chats:
            print("Этому боту ещё никто не писал: откройте именно его в Telegram, нажмите Start и отправьте любое сообщение, затем повторите.")
            return
        print("Чаты, писавшие боту (значение для секрета TELEGRAM_CHAT_ID):")
        for c in chats:
            print(f"  chat_id={c['chat_id']}  {c['type']}  {c['name']}  «{c['last_text']}»")
        return
    text = open(args.file, encoding="utf-8").read() if args.file else (args.text or "")
    if not text.strip():
        raise SystemExit("нечего отправлять: --file или --text")
    n = telegram_send(strip_markdown(text))
    print(f"отправлено в Telegram: {n} сообщ.")


def cmd_backtest(args, settings):
    from .backtest.engine import BacktestEngine
    from .data.cache import SqliteCache
    from .data.moex import MoexClient
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    name, params = _strategy_spec(args, settings)
    st = make_strategy(name, params)
    if args.fixtures:
        raise SystemExit("бэктест на фикстурах не поддерживается: нужна история MOEX (без --fixtures)")
    cache = SqliteCache(settings.get("data", "cache_path", default="data/cache/http_cache.sqlite"))
    client = MoexClient(cache=cache)
    from .backtest.data import MoexHistoryProvider
    provider = MoexHistoryProvider(client, cache, load_curves=args.curves)
    snap = load_snapshot(settings, None, client)
    if args.universe:
        ids = {s.strip().upper() for s in args.universe.split(",")}
        bonds = [b for b, _ in snap.universe if b.secid in ids or b.isin in ids]
    else:
        rows = _screen(snap, settings, args)
        bonds = [r.bond for r in rows[: args.auto]]
        print(f"Вселенная: top-{len(bonds)} текущего скрина (внимание: survivorship bias)", file=sys.stderr)
    if not bonds:
        raise SystemExit("пустая вселенная")
    eng = BacktestEngine(st, bonds, provider, start, end,
                         initial_cash=settings.get("backtest", "initial_cash", default=1_000_000),
                         commission_bp=settings.get("backtest", "commission_bp", default=5),
                         slippage_bp=settings.get("backtest", "slippage_bp", default=5),
                         rebalance=args.rebalance or settings.get("strategy", "rebalance", default="monthly"),
                         screener_cfg=ScreenerConfig.from_dict({**settings.get("screener", default={}), "min_turnover": 0, "max_bid_ask_pct": 100}),
                         risk_limits=RiskLimits.from_dict(settings.get("risk", default={})),
                         benchmark=None if (args.benchmark or "").lower() == "none" else (args.benchmark or settings.get("backtest", "benchmark", default="RGBITR")),
                         cash_spread_bp=settings.get("backtest", "cash_spread_bp", default=-50))
    res = eng.run()
    print(f"Стратегия {name} {params or ''}, {start} — {end}, ребалансировка {eng.rebalance}, бумаг {len(bonds)}\n")
    for k, v in res.summary().items():
        print(f"{k:24} {v:.2f}" if isinstance(v, float) else f"{k:24} {v}")
    if args.csv:
        out = pd.DataFrame({"nav": res.nav})
        if res.benchmark is not None:
            out["benchmark"] = res.benchmark.reindex(out.index).ffill()
        out.to_csv(args.csv)
        print(f"\nNAV сохранён: {args.csv}")
    if args.trades:
        print("\nСделки:")
        print(pd.DataFrame([t.__dict__ for t in res.trades]).to_string(index=False))


def cmd_sandbox_init(args, settings):
    from .execution.tinvest import TInvestBroker
    t = settings.get("execution", "tinvest", default={})
    br = TInvestBroker(settings.tinvest_token, sandbox=True, account_id="", ca_bundle=t.get("ca_bundle") or None)
    acc = br.sandbox_open(0.0, reuse=not args.new)
    has_positions = bool(br.positions())
    if has_positions and not args.topup:
        # счёт уже торгует: пополнение исказило бы учёт результата (NAV рос бы от каждого запуска)
        print(f"Счёт песочницы {acc}: есть позиции, пополнение пропущено (добавьте --topup, чтобы довести деньги до {args.amount:,.0f}). "
              f"Денег на счёте: {br.cash():,.0f} руб.")
        return
    br.sandbox_open(args.amount, reuse=True)
    print(f"Счёт песочницы {acc} готов, деньги доведены до {args.amount:,.0f} руб. Денег на счёте: {br.cash():,.0f} руб. "
          f"Если счетов несколько — укажите id в config.yaml -> execution.tinvest.account_id")


def cmd_ratings(args, settings):
    from .data.ratings import RatingsBook
    if args.action == "discover":
        from .data.ratings_web import discover, discover_deep
        if args.deep:
            discover_deep(args.names or None)
        else:
            discover(args.names)
        return
    if args.action == "fetch":
        from .data.ratings_web import load_acra_press, load_nkr, load_nkr_tables, load_raexpert
        path = settings.get("data", "ratings_csv", default="data/ratings.csv")
        book = RatingsBook.from_csv(path)
        before = len(book)
        got: list = []
        sources = args.sources or ["nkr", "raexpert", "acra"]
        if "nkr" in sources:
            got += load_nkr_tables()           # актуальное состояние — таблицы эмитентов/эмиссий
            if args.press:
                got += load_nkr(pages=args.pages)
        if "raexpert" in sources:
            got += load_raexpert()
        if "acra" in sources:
            got += load_acra_press(max_pages=args.pages if args.pages > 3 else 120)
        seen = {(r.subject, r.agency, r.kind, r.isin, r.date) for r in book.all}
        added = 0
        for r in got:
            key = (r.subject, r.agency, r.kind, r.isin, r.date)
            if key not in seen:
                book.add(r); seen.add(key); added += 1
        book.to_csv(path)
        by_agency = {}
        for r in book.all:
            by_agency[r.agency] = by_agency.get(r.agency, 0) + 1
        print(f"Добавлено {added} записей, всего {len(book)} -> {path}; по агентствам: {by_agency}")
        return
    book = RatingsBook.from_csv(settings.get("data", "ratings_csv", default="data/ratings.csv"))
    if args.action == "show":
        snap = load_snapshot(settings, args.fixtures)
        pair = next(((b, q) for b, q in snap.universe if b.secid == args.secid.upper() or b.isin == args.secid.upper()), None)
        if not pair:
            raise SystemExit(f"{args.secid}: не найдена")
        bond = pair[0]
        r = book.lookup(bond)
        print(f"{bond.secid} {bond.name}: {r.rating + ' (' + r.agency + (', ' + str(r.date) if r.date else '') + ')' if r else 'рейтинг не найден'}")
        cands = book.candidates(bond)
        if len(cands) > 1:
            print("Все записи: " + "; ".join(f"{c.agency} {c.rating} {c.date or ''}" for c in cands))
        return
    if args.action == "list":
        print(f"Книга рейтингов: {len(book)} записей")
        print(pd.DataFrame([{"subject": r.subject, "agency": r.agency, "rating": r.rating, "date": r.date, "kind": r.kind,
                             "isin": r.isin, "alias": r.alias} for r in book.all]).to_string(index=False) if len(book) else "(пусто)")
        return
    if args.action == "coverage":
        snap = load_snapshot(settings, args.fixtures)
        rows = _screen(snap, settings, args)
        rated = [r for r in rows if r.rating is not None]
        print(f"В скрине {len(rows)} бумаг, с рейтингом {len(rated)}, без рейтинга {len(rows) - len(rated)}")
        missing = [r for r in rows if r.rating is None]
        if missing:
            print("Без рейтинга (для добавления alias в CSV):")
            print(pd.DataFrame([{"secid": r.secid, "name": r.bond.name, "full_name": r.bond.full_name[:40], "isin": r.bond.isin,
                                 "ytw": round(r.metrics.yield_worst, 2)} for r in missing]).to_string(index=False))
        return


def cmd_financials(args, settings):
    from .data.financials import FinancialsBook, IssuerMap, IssuerRecord, implied_grade
    fin_path = settings.get("data", "financials_csv", default="data/financials.csv")
    iss_path = settings.get("data", "issuers_csv", default="data/issuers.csv")
    cache_dir = settings.get("data", "financials_cache", default="data/financials")
    if args.action == "discover":
        from .data.financials import discover
        discover(args.query or "Балтийский лизинг")
        return
    if args.action == "fetch":
        from .data.girbo import GirboClient, GirboUnavailable, fetch_issuer
        book, issuers = FinancialsBook.from_csv(fin_path), IssuerMap.from_csv(iss_path)
        queries: list[tuple[str, Optional[object]]] = [(q, None) for q in (args.query or [])]
        if args.from_screen:
            snap = load_snapshot(settings, args.fixtures)
            rows = _screen(snap, settings, args)
            seen: set[str] = set()
            for r in rows:
                if r.bond.is_ofz:
                    continue
                key = r.inn or r.bond.issuer_key
                if key in seen:
                    continue
                seen.add(key)
                queries.append((r.inn or r.bond.full_name or r.bond.name, r.bond))
        client = GirboClient()
        ok, failed = 0, 0
        for q, bond in queries:
            try:
                org, sts = fetch_issuer(client, q, cache_dir=cache_dir, years=args.years, refresh=args.refresh)
            except GirboUnavailable as e:
                print(f"ГИР БО недоступен: {e}\nЗапустите команду из РФ (или через российский прокси) — из-за рубежа сайт отдаёт заглушку.")
                break
            except Exception as e:  # noqa: BLE001
                print(f"{q}: ошибка {e}"); failed += 1
                continue
            if not org:
                print(f"{q}: организация не найдена"); failed += 1
                continue
            for st in sts:
                book.add(st)
            inn = str(org.get("inn") or "")
            if inn and bond is not None and issuers.lookup(bond) is None:
                issuers.add(IssuerRecord(inn, org.get("shortName") or org.get("fullName") or "", alias=bond.name.split()[0] if bond.name else "",
                                         emitter_id=""))
            ok += 1
            latest = book.metrics(inn) if inn else None
            print(f"{q}: {org.get('shortName') or org.get('fullName')} ИНН {inn}: отчётов {len(sts)}"
                  + (f", последний {latest.year}: балл {latest.score:.0f} ({implied_grade(latest.score)}) {'; '.join(latest.flags)}" if latest else ""))
        book.to_csv(fin_path); issuers.to_csv(iss_path)
        print(f"Готово: {ok} эмитентов загружено, {failed} с ошибками; книга: {book.issuers} эмитентов / {len(book)} отчётов -> {fin_path}; карта ИНН: {len(issuers)} -> {iss_path}")
        return
    book = FinancialsBook.from_csv(fin_path)
    if args.action == "show":
        inn = args.query[0] if args.query else ""
        if not inn.isdigit():
            issuers = IssuerMap.from_csv(iss_path)
            hit = next((r for r in issuers.records if inn.lower() in (r.name + " " + r.alias).lower()), None)
            if not hit:
                raise SystemExit(f"{inn}: не найден в карте ИНН {iss_path}")
            inn = hit.inn
        m = book.metrics(inn)
        if not m:
            raise SystemExit(f"ИНН {inn}: нет отчётности в {fin_path}")
        print(f"ИНН {inn}, {m.year}: балл {m.score:.0f} (≈{implied_grade(m.score)})")
        print(f"  выручка {m.revenue:,.0f}  EBIT {m.ebit:,.0f}  чистая прибыль {m.net_income:,.0f}  капитал {m.equity:,.0f}  (тыс. руб.)")
        print(f"  долг {m.total_debt:,.0f}  чистый долг {m.net_debt:,.0f}  деньги {m.cash:,.0f}  короткий долг {m.short_debt_share:.0%}  проценты {m.interest_expense:,.0f}")
        def f(x, fmt="{:.2f}"): return "н/д" if x is None else fmt.format(x)
        print(f"  покрытие процентов {f(m.interest_coverage)}x  чистый долг/EBIT {f(m.net_debt_to_ebit)}x  обязательства/капитал {f(m.liabilities_to_equity)}x  "
              f"текущая ликвидность {f(m.current_ratio)}  деньги/короткий долг {f(m.cash_to_short_debt)}")
        print("  флаги: " + ("; ".join(m.flags) if m.flags else "нет"))
        return
    if args.action == "coverage":
        snap = load_snapshot(settings, args.fixtures)
        rows = _screen(snap, settings, args)
        corp = [r for r in rows if not r.bond.is_ofz]
        with_inn = [r for r in corp if r.inn]
        with_fin = [r for r in corp if r.fin is not None]
        print(f"В скрине {len(corp)} корпоративных бумаг: с ИНН {len(with_inn)}, с отчётностью {len(with_fin)}")
        missing = [r for r in corp if r.fin is None]
        if missing:
            print("Без отчётности (financials fetch --from-screen загрузит по названию/ИНН):")
            print(pd.DataFrame([{"secid": r.secid, "name": r.bond.name, "full_name": r.bond.full_name[:40], "inn": r.inn, "sector": r.sector,
                                 "rating": r.rating_str} for r in missing]).to_string(index=False))
        return


def cmd_disclosure(args, settings):
    from .data.disclosure import EdisclosureClient, EventsBook, discover
    path = settings.get("data", "disclosure_csv", default="data/disclosure.csv")
    if args.action == "discover":
        discover(args.query[0] if args.query else "Балтийский лизинг", browser=args.browser)
        return
    book = EventsBook.from_csv(path)
    if args.action == "list":
        evs = [e for e in book.events if not args.kind or e.kind in args.kind]
        print(f"Книга событий: {len(book)} записей" + (f", показано {len(evs)}" if args.kind else ""))
        if evs:
            print(pd.DataFrame([{"date": e.date, "issuer": e.issuer[:30], "inn": e.inn, "kind": e.kind, "title": e.title[:90]} for e in evs]).to_string(index=False))
        return
    if args.action == "fetch":
        from datetime import timedelta
        from .data.financials import IssuerMap
        queries: list[tuple[str, str]] = [(q, "") for q in (args.query or [])]
        if args.from_screen:
            snap = load_snapshot(settings, args.fixtures)
            rows = _screen(snap, settings, args)
            seen: set[str] = set()
            for r in rows:
                if r.bond.is_ofz or r.bond.issuer_key in seen:
                    continue
                seen.add(r.bond.issuer_key)
                queries.append((r.inn or r.bond.full_name or r.bond.name, r.inn))
        client = EdisclosureClient(browser=args.browser)
        since = date.today() - timedelta(days=args.days)
        added = 0
        try:
            for q, inn in queries:
                try:
                    found = client.search(q)
                except Exception as e:  # noqa: BLE001
                    print(f"{q}: ошибка поиска {e}")
                    continue
                if not found:
                    print(f"{q}: компания не найдена")
                    continue
                c = found[0]
                try:
                    evs = client.events(c["id"], pages=args.pages, since=since)
                except Exception as e:  # noqa: BLE001
                    print(f"{q}: ошибка ленты {e}")
                    continue
                n = 0
                for e in evs:
                    e.inn = e.inn or c.get("inn") or inn
                    e.issuer = e.issuer or c["name"]
                    n += book.add(e)
                added += n
                stops = [e for e in evs if e.kind in ("default", "tech_default", "restructuring")]
                print(f"{q}: {c['name']} (id {c['id']}): фактов {len(evs)}, новых {n}, стоп-факторов {len(stops)}")
        finally:
            client.close()
        book.to_csv(path)
        print(f"Добавлено {added} событий, всего {len(book)} -> {path}")
        return


def cmd_news(args, settings):
    from .data.news import NewsBook, discover, scan_general_feeds, search_news
    path = settings.get("data", "news_csv", default="data/news.csv")
    if args.action == "discover":
        discover(args.query[0] if args.query else "Балтийский лизинг")
        return
    book = NewsBook.from_csv(path)
    if args.action == "list":
        items = book.items if not args.query else book.for_issuer(name=args.query[0])
        items = [it for it in items if not args.negative or it.score < 0]
        print(f"Книга новостей: {len(book)} записей, показано {len(items)}")
        if items:
            print(pd.DataFrame([{"date": it.date, "query": it.query[:25], "score": it.score, "tags": it.tags, "title": it.title[:90],
                                 "source": it.source[:20]} for it in sorted(items, key=lambda x: x.date, reverse=True)[: args.top]]).to_string(index=False))
        return
    if args.action == "show":
        if not args.query:
            raise SystemExit("укажите --query <эмитент>")
        ns = book.issuer_score(date.today(), name=args.query[0], days=args.days)
        print(f"{args.query[0]}: {ns.describe()}")
        for it in ns.items[:20]:
            print(f"  {it.date} [{it.score:+.1f} {it.tags or '-'}] {it.title[:100]}")
        return
    if args.action == "fetch":
        names: list[tuple[str, str]] = [(q, "") for q in (args.query or [])]
        if args.from_screen:
            snap = load_snapshot(settings, args.fixtures)
            rows = _screen(snap, settings, args)
            seen: set[str] = set()
            for r in rows:
                if r.bond.is_ofz or r.bond.issuer_key in seen:
                    continue
                seen.add(r.bond.issuer_key)
                names.append((r.bond.full_name or r.bond.name, r.inn))
        added, neg = 0, 0
        for name, inn in names:
            items = search_news(name, sources=args.sources or ("google", "bing"), days=args.days, inn=inn)
            n = sum(book.add(it) for it in items)
            added += n
            bad = [it for it in items if it.score <= -3]
            neg += len(bad)
            print(f"{name[:40]}: найдено {len(items)}, новых {n}" + (f", сильный негатив: {bad[0].title[:70]}" if bad else ""))
        if args.general:
            gen = scan_general_feeds([n for n, _ in names], days=min(args.days, 30))
            g_added = sum(book.add(it) for it in gen)
            added += g_added
            print(f"Общие ленты: {len(gen)} совпадений, новых {g_added}")
        book.prune()
        book.to_csv(path)
        print(f"Добавлено {added} новостей (сильный негатив: {neg}), всего {len(book)} -> {path}")
        return


def _rating_change(snap: MarketSnapshot, bond) -> str:
    """«↓ BBB→BB+ (2026-08-28, Эксперт РА)» если последнее действие агентства изменило ступень за 180 дней."""
    if snap.ratings is None or bond.is_ofz:
        return ""
    from .data.ratings import GRADE
    cands = sorted((r for r in snap.ratings.candidates(bond) if r.date), key=lambda r: r.date)
    if len(cands) < 2:
        return ""
    last = cands[-1]
    if (snap.settle - last.date).days > 180:
        return ""
    prev = next((r for r in reversed(cands[:-1]) if r.agency == last.agency), None) or cands[-2]
    if GRADE[prev.rating] == GRADE[last.rating]:
        return ""
    arrow = "↓" if GRADE[last.rating] > GRADE[prev.rating] else "↑"
    return f"{arrow} {prev.rating}→{last.rating} ({last.date}, {last.agency})"


def cmd_spreads(args, settings):
    """Шаги 2–3 алгоритма: спреды по ступеням рейтинга и бумаги, за которые рынок требует больше, чем за похожие."""
    from .analytics.fair_spread import features_of, fit_fair_spread
    from .data.ratings import GRADE, SCALE
    snap = load_snapshot(settings, args.fixtures)
    rows = _screen(snap, settings, args)
    _header(snap)
    corp = [r for r in rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None]
    model = fit_fair_spread(corp)
    print(f"Модель справедливого спреда: {model.describe()}\n")
    # --- таблица по ступеням ---
    import statistics
    buckets: dict[str, list] = {}
    for r in corp:
        buckets.setdefault(r.rating.rating if r.rating else "—", []).append(r)
    order = {g: i for i, g in enumerate(SCALE)}
    recs = []
    for g, rs in sorted(buckets.items(), key=lambda kv: order.get(kv[0], 99)):
        sp = [r.metrics.g_spread for r in rs]
        recs.append({"rating": g, "n": len(rs), "spread_med": round(statistics.median(sp)), "spread_p25": round(sorted(sp)[len(sp) // 4]),
                     "spread_p75": round(sorted(sp)[(3 * len(sp)) // 4]), "ytw_med": round(statistics.median(r.metrics.yield_worst for r in rs), 1),
                     "dur_med": round(statistics.median(r.metrics.macaulay_duration for r in rs), 1)})
    print("Спреды к кривой ОФЗ по ступеням рейтинга (б.п.):")
    print(pd.DataFrame(recs).to_string(index=False))
    # --- остатки: за что платят больше внутри ступени ---
    out = []
    for r in corp:
        if args.rating and (r.rating.rating if r.rating else "—") != args.rating.upper():
            continue
        g = r.rating.rating if r.rating else "—"
        peers = [x.metrics.g_spread for x in buckets[g]]
        med = statistics.median(peers)
        resid_model = model.residual(features_of(r), r.metrics.g_spread) if model.ok else float("nan")
        why = []
        if r.news is not None and r.news.n:
            why.append(f"новости {r.news.score:+.1f}" + (f" ({r.news.worst.tags.split(',')[0]})" if r.news.worst else ""))
        ch = _rating_change(snap, r.bond)
        if ch:
            why.append("рейтинг " + ch)
        if r.fin is not None:
            why.append(f"отчётность {r.fin.score:.0f}" + (": " + "; ".join(r.fin.flags[:2]) if r.fin.flags else ""))
        if r.stop_events:
            why.append(f"факт {r.stop_events[-1].kind}")
        if r.bond.has_offer and r.bond.offer_date and r.bond.offer_date > snap.settle:
            why.append(f"оферта {r.bond.offer_date}")
        if r.bond.has_amortization:
            why.append("амортизация")
        if r.bond.list_level == 3:
            why.append("3-й уровень")
        if r.quote.turnover < 3e6:
            why.append(f"оборот {r.quote.turnover / 1e6:.1f} млн")
        out.append({"secid": r.secid, "name": r.bond.name, "rating": g, "dur": round(r.metrics.macaulay_duration, 1),
                    "ytw": round(r.metrics.yield_worst, 1), "spread": round(r.metrics.g_spread), "vs_peers": round(r.metrics.g_spread - med),
                    "vs_model": None if resid_model != resid_model else round(resid_model), "sector": r.sector, "why": "; ".join(why) or "—"})
    df = pd.DataFrame(out)
    if df.empty:
        print("\n(нет корпоративных бумаг)")
        return
    df = df.sort_values("vs_peers", ascending=False)
    print(f"\nЗа что платят больше, чем за соседей по ступени (топ {args.top}; vs_peers — к медиане ступени, vs_model — к регрессии):")
    _print_df(df.head(args.top), csv=args.csv)
    if args.bottom:
        print(f"\nЗа что платят меньше (топ {args.bottom}):")
        print(df.tail(args.bottom).iloc[::-1].to_string(index=False))


def cmd_strategies(args, settings):
    for name, cls in STRATEGIES.items():
        doc = (cls.__doc__ or "").strip().splitlines()[0]
        print(f"{name:12} {doc}")


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    def add_common(pp, suppress: bool):
        d = argparse.SUPPRESS if suppress else None
        pp.add_argument("--config", "-c", default=d, help="путь к config.yaml (по умолчанию ./config.yaml)")
        pp.add_argument("--fixtures", default=d, help="офлайн-режим: каталог с bonds_board.json/zcyc.json/cbr_keyrate.xml")
        pp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS if suppress else False)

    p = argparse.ArgumentParser(prog="bondtrader", description="Торговая система для облигаций MOEX")
    add_common(p, suppress=False)
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    add_common(common, suppress=True)   # те же флаги допустимы и после подкоманды
    sub = p.add_subparsers(dest="cmd", required=True)

    def screen_opts(sp):
        sp.add_argument("--ofz-only", action="store_true")
        sp.add_argument("--corporate-only", action="store_true")
        sp.add_argument("--include-floaters", action="store_true")
        sp.add_argument("--min-turnover", type=float)
        sp.add_argument("--max-duration", type=float)
        sp.add_argument("--min-rating", help="минимальный рейтинг, напр. BB-")

    def strat_opts(sp):
        sp.add_argument("--strategy", "-s", choices=list(STRATEGIES))
        sp.add_argument("--param", "-p", action="append", help="параметр стратегии key=value (можно несколько)")

    sp = sub.add_parser("screen", help="скринер облигаций", parents=[common]); screen_opts(sp)
    sp.add_argument("--top", type=int, default=40); sp.add_argument("--csv"); sp.set_defaults(fn=cmd_screen)
    sp = sub.add_parser("curve", parents=[common], help="кривая ОФЗ"); sp.set_defaults(fn=cmd_curve)
    sp = sub.add_parser("spreads", parents=[common], help="спреды по ступеням рейтинга и бумаги с премией к соседям (шаги 2–3 алгоритма)"); screen_opts(sp)
    sp.add_argument("--rating", help="только одна ступень, напр. BBB"); sp.add_argument("--top", type=int, default=40); sp.add_argument("--bottom", type=int, default=0)
    sp.add_argument("--csv"); sp.set_defaults(fn=cmd_spreads)
    sp = sub.add_parser("keyrate", parents=[common], help="ключевая ставка ЦБ и фаза цикла"); sp.set_defaults(fn=cmd_keyrate)
    sp = sub.add_parser("bond", parents=[common], help="карточка облигации"); sp.add_argument("secid"); sp.add_argument("--schedule", action="store_true"); sp.set_defaults(fn=cmd_bond)
    sp = sub.add_parser("strategies", parents=[common], help="список стратегий"); sp.set_defaults(fn=cmd_strategies)
    sp = sub.add_parser("financials", parents=[common], help="отчётность эмитентов (ГИР БО): fetch | show | coverage | discover")
    sp.add_argument("action", choices=["fetch", "show", "coverage", "discover"])
    sp.add_argument("--query", nargs="*", help="ИНН или названия эмитентов (fetch/show/discover)")
    sp.add_argument("--from-screen", action="store_true", help="fetch: все эмитенты из текущего скрина")
    sp.add_argument("--years", type=int, default=3); sp.add_argument("--refresh", action="store_true", help="fetch: игнорировать кэш")
    screen_opts(sp); sp.set_defaults(fn=cmd_financials)
    sp = sub.add_parser("disclosure", parents=[common], help="существенные факты e-disclosure: fetch | list | discover")
    sp.add_argument("action", choices=["fetch", "list", "discover"])
    sp.add_argument("--query", nargs="*", help="ИНН или названия эмитентов")
    sp.add_argument("--from-screen", action="store_true"); sp.add_argument("--browser", action="store_true", help="через Playwright/Chromium")
    sp.add_argument("--pages", type=int, default=3); sp.add_argument("--days", type=int, default=730)
    sp.add_argument("--kind", nargs="*", help="list: фильтр по типу (default, tech_default, restructuring, coupon, rating, ...)")
    screen_opts(sp); sp.set_defaults(fn=cmd_disclosure)
    sp = sub.add_parser("news", parents=[common], help="новостной фон эмитентов: fetch | list | show | discover")
    sp.add_argument("action", choices=["fetch", "list", "show", "discover"])
    sp.add_argument("--query", nargs="*", help="названия эмитентов"); sp.add_argument("--from-screen", action="store_true")
    sp.add_argument("--sources", nargs="*", choices=["google", "bing"]); sp.add_argument("--general", action="store_true", help="fetch: также общие ленты Интерфакс/РБК/Коммерсант/Финам")
    sp.add_argument("--days", type=int, default=120); sp.add_argument("--negative", action="store_true", help="list: только негатив"); sp.add_argument("--top", type=int, default=60)
    screen_opts(sp); sp.set_defaults(fn=cmd_news)
    sp = sub.add_parser("ratings", parents=[common], help="кредитные рейтинги: list | show SECID | coverage | discover")
    sp.add_argument("action", choices=["list", "show", "coverage", "discover", "fetch"]); sp.add_argument("secid", nargs="?")
    sp.add_argument("--names", nargs="*", help="для discover: какие источники смотреть (для --deep: список URL)")
    sp.add_argument("--deep", action="store_true", help="для discover: формы, пагинация, ajax")
    sp.add_argument("--pages", type=int, default=3, help="для fetch --press: сколько страниц пресс-релизов НКР")
    sp.add_argument("--press", action="store_true", help="для fetch: дополнительно разобрать пресс-релизы НКР")
    sp.add_argument("--sources", nargs="*", choices=["nkr", "raexpert", "acra"], help="для fetch: источники (по умолчанию все)"); screen_opts(sp); sp.set_defaults(fn=cmd_ratings)
    sp = sub.add_parser("signals", parents=[common], help="целевой портфель и ордера по стратегии"); screen_opts(sp); strat_opts(sp)
    sp.add_argument("--broker", choices=["paper", "tinvest"]); sp.add_argument("--csv"); sp.set_defaults(fn=cmd_signals)
    sp = sub.add_parser("trade", parents=[common], help="исполнить ребалансировку через брокера"); screen_opts(sp); strat_opts(sp)
    sp.add_argument("--keep-orders", action="store_true", help="не снимать старые активные заявки перед ребалансировкой")
    sp.add_argument("--broker", choices=["paper", "tinvest"]); sp.add_argument("--confirm", action="store_true", help="реально отправить ордера (иначе dry-run)")
    sp.add_argument("--live", action="store_true", help="боевой контур T-Invest вместо песочницы"); sp.set_defaults(fn=cmd_trade)
    sp = sub.add_parser("monitor", parents=[common], help="алерты по держащимся позициям (стоп-факторы, просадки, новости)"); screen_opts(sp)
    sp.add_argument("--broker", choices=["paper", "tinvest"]); sp.add_argument("--live", action="store_true")
    sp.add_argument("--strict", action="store_true", help="код возврата 2 при критических алертах"); sp.set_defaults(fn=cmd_monitor)
    sp = sub.add_parser("report", parents=[common], help="ежедневный отчёт (Markdown): рынок, портфель, алерты, цель, ордера"); screen_opts(sp); strat_opts(sp)
    sp.add_argument("--broker", choices=["paper", "tinvest"]); sp.add_argument("--live", action="store_true")
    sp.add_argument("--md", help="сохранить в файл"); sp.add_argument("--telegram", action="store_true", help="отправить в Telegram (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
    sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("notify", parents=[common], help="отправить текст/файл в Telegram"); sp.add_argument("--file"); sp.add_argument("--text")
    sp.add_argument("--whoami", action="store_true", help="показать chat_id тех, кто писал боту (для секрета TELEGRAM_CHAT_ID)"); sp.set_defaults(fn=cmd_notify)
    sp = sub.add_parser("portfolio", parents=[common], help="состояние портфеля и риск-метрики"); screen_opts(sp)
    sp.add_argument("--broker", choices=["paper", "tinvest"]); sp.set_defaults(fn=cmd_portfolio)
    sp = sub.add_parser("backtest", parents=[common], help="бэктест стратегии на истории MOEX"); screen_opts(sp); strat_opts(sp)
    sp.add_argument("--start", required=True, help="YYYY-MM-DD"); sp.add_argument("--end")
    sp.add_argument("--universe", help="список SECID через запятую (иначе top-N текущего скрина)")
    sp.add_argument("--auto", type=int, default=15, help="размер автоматической вселенной")
    sp.add_argument("--rebalance", choices=["daily", "weekly", "monthly", "quarterly"])
    sp.add_argument("--benchmark", help="индекс MOEX, напр. RGBITR или RUCBITR; none — без бенчмарка")
    sp.add_argument("--curves", action="store_true", help="грузить исторические кривые zcyc с MOEX (медленнее)")
    sp.add_argument("--csv", help="сохранить NAV в CSV"); sp.add_argument("--trades", action="store_true"); sp.set_defaults(fn=cmd_backtest)
    sp = sub.add_parser("sandbox-init", parents=[common], help="открыть и пополнить счёт песочницы T-Invest"); sp.add_argument("--amount", type=float, default=1_000_000)
    sp.add_argument("--topup", action="store_true", help="пополнить даже если на счёте уже есть позиции")
    sp.add_argument("--new", action="store_true", help="открыть новый счёт, даже если уже есть открытый")
    sp.set_defaults(fn=cmd_sandbox_init)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.load(args.config)
    try:
        rc = args.fn(args, settings)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # вывод в head/less
        try:
            sys.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        return 0
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())

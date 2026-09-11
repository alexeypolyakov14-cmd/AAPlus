"""Мониторинг позиций, отчёт, уведомления, рыночные заявки песочницы."""
import json
import os
from datetime import date

from bondtrader.cli import main
from bondtrader.data.news import NewsBook, NewsItem, NewsScore
from bondtrader.monitor import Alert, check_positions, worst_level
from bondtrader.notify import _split, strip_markdown
from bondtrader.portfolio import Portfolio, Position

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _row(secid, price=100.0, g_spread=300.0, news=None, rating=None):
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow
    b = Bond(secid, name=secid)
    q = Quote(secid, date(2025, 6, 2), price=price, turnover=5e6)
    m = BondMetrics(secid, price, price * 10, 20.0, None, 20.0, 1.5, 1.3, 0, 0.1, 1.5, 15, g_spread)
    return ScreenRow(b, q, m, news=news, rating=Rating("x", "y", rating) if rating else None)


def test_check_positions_levels():
    pf = Portfolio(cash=0)
    for s in ("A", "B", "C", "D", "E"):
        pf.positions[s] = Position(s, 10, 100.0)
    bad_news = NewsScore(n=2, negative=2, score=-5.0)
    bad_news.worst = NewsItem(date(2025, 6, 1), "B", "google:РБК", "B: убыток", "", "", -1.5, "loss")
    stop_news = NewsScore(n=1, negative=1, score=-4.0)
    stop_news.stop = NewsItem(date(2025, 6, 1), "C", "google:Интерфакс", "C допустила дефолт", "", "", -4.0, "default")
    rows = {"A": _row("A", price=93.0), "B": _row("B", news=bad_news, g_spread=1200), "C": _row("C", news=stop_news),
            "D": _row("D", rating="CCC")}
    alerts = check_positions(pf, rows, {"A": "низкий оборот", "C": "новости: default 2025-06-01"},
                             max_g_spread_bp=800, price_drop_pct=5, news_alert_score=-3, min_rating="B", open_orders=3)
    by = {}
    for a in alerts:
        by.setdefault(a.secid, []).append(a.level)
    assert by["A"] == ["warning", "warning"]                 # выпала по обороту + просадка 7%
    assert set(by["B"]) == {"warning"} and len(by["B"]) == 2  # новости + спред
    assert "critical" in by["C"] and "critical" in by["D"]   # новость СМИ о дефолте; рейтинг ниже B
    assert by["E"] == ["warning"]                            # нет котировки
    assert any(a.level == "info" and "заявок" in a.message for a in alerts)
    assert worst_level(alerts) == "critical" and alerts[0].level == "critical"
    assert worst_level([]) is None and str(Alert("info", "", "x")) == "[~] x"


def test_notify_helpers():
    md = "# Заголовок\n**жирный** и `код`\n```\nblock\n```\nстрока"
    assert strip_markdown(md) == "Заголовок\nжирный и код\nстрока"
    parts = _split("a\n" * 10, limit=5)
    assert all(len(p) <= 5 for p in parts) and "".join(p.replace("\n", "") for p in parts) == "a" * 10
    parts = _split("<pre>x\ny</pre>\n\nsecond block\n\nthird", limit=20)
    assert parts[0] == "<pre>x\ny</pre>" and parts[1] == "second block" and parts[2] == "third"


def test_monitor_and_report_cli(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "news.csv").write_text(
        "date,query,inn,source,title,url,score,tags\n2025-06-01,ВИС Ф БП04,,google:Интерфакс,ВИС Финанс допустила дефолт по купону,,-4,default\n", encoding="utf-8")
    cfg.write_text(f"""
data:
  news_csv: {tmp_path / 'data' / 'news.csv'}
  ratings_csv: ''
execution:
  broker: paper
  state_path: {tmp_path / 'pf.json'}
  journal_path: {tmp_path / 'o.jsonl'}
backtest:
  initial_cash: 2000000
risk:
  max_weight_per_bond: 0.3
""", encoding="utf-8")
    # покупаем бумагу, которая затем получает новость о дефолте
    (tmp_path / "pf.json").write_text(json.dumps({"cash": 1000000, "positions": {"RU000A103WV8": {"secid": "RU000A103WV8", "qty": 100, "avg_price": 95.0, "face": 1000}},
                                                    "realized_pnl": 0, "coupons_received": 0, "commissions_paid": 0}), encoding="utf-8")
    assert main(["-c", str(cfg), "--fixtures", FIX, "monitor"]) == 0
    out = capsys.readouterr().out
    assert "[!!] RU000A103WV8" in out and "default" in out
    assert main(["-c", str(cfg), "--fixtures", FIX, "monitor", "--strict"]) == 2
    capsys.readouterr()
    md_path = tmp_path / "r.md"
    assert main(["-c", str(cfg), "--fixtures", FIX, "report", "-s", "carry", "--md", str(md_path)]) == 0
    md = md_path.read_text(encoding="utf-8")
    assert md.startswith("# 🔴") and "## Алерты" in md and "## Позиции" in md and "RU000A103WV8" in md
    assert "Отсев по стоп-факторам" in md and "Негативные новости" in md
    # Telegram-версия: HTML с <pre>-таблицами, без Markdown-таблиц
    tg_path = tmp_path / "r.html"
    assert main(["-c", str(cfg), "--fixtures", FIX, "report", "-s", "carry", "--tg-file", str(tg_path)]) == 0
    tg = tg_path.read_text(encoding="utf-8")
    assert tg.startswith("🔴 <b>bondtrader") and "<pre>" in tg and "|---" not in tg and "P&amp;L" in tg and "Алерты" in tg
    from bondtrader.notify import _split
    parts = _split(tg, limit=600)
    assert all(len(p) <= 600 for p in parts) and all(p.count("<pre>") == p.count("</pre>") for p in parts)

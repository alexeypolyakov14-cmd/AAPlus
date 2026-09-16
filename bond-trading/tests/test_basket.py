"""Корзина: накопительная книга (add/set/log), CLI basket, секция в отчёте и боте."""
import json
import os
from datetime import date

import pytest

from bondtrader.basket import BasketBook, basket_rows
from bondtrader.cli import main

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def test_book_is_cumulative(tmp_path):
    p = tmp_path / "basket.csv"
    b = BasketBook([], str(p))
    ev = b.add("RU000A103WV8", "RU000A103WV8", "ВИС Ф БП04", "B", 20, note="первичка", on=date(2026, 9, 14))
    assert ev.action == "add" and "B 20%" in ev.detail and b.log_path.endswith("basket_log.csv")
    b.add("RU000A104ZK2", "RU000A104ZK2", "МТС 1P-21", "A", 15, on=date(2026, 9, 14))
    # смена статуса и веса — две записи журнала, запись остаётся в книге
    ev = b.update("RU000A103WV8", status="hold", weight=10, note="дефолт по купону", on=date(2026, 9, 15))
    assert ev.action == "weight" and b.by_isin("RU000A103WV8").status == "hold" and len(b.log) == 4
    assert "15.09: дефолт по купону" in b.by_isin("RU000A103WV8").note
    # повторное «добавить» уже известной бумаги = вернуть в портфель, история не теряется
    b.add("RU000A103WV8", "", "", "B", 20, note="оплатили купон", on=date(2026, 9, 20))
    e = b.by_isin("RU000A103WV8")
    assert e.status == "active" and e.weight == 20 and e.added == date(2026, 9, 14) and e.changed == date(2026, 9, 20)
    assert b.update("RU000A103WV8", status="active", on=date(2026, 9, 21)) is None   # без изменений — без записи
    b.update("RU000A104ZK2", status="removed", note="продали", on=date(2026, 9, 22))
    assert [x.isin for x in b.live()] == ["RU000A103WV8"] and len(b.entries) == 2
    b.save()
    b2 = BasketBook.from_csv(str(p))
    assert len(b2) == 2 and len(b2.log) == len(b.log) and b2.by_isin("RU000A104ZK2").status == "removed"
    assert b2.find("вис")[0].isin == "RU000A103WV8" and b2.find("RU000A104ZK2, мтс") and "исключено 1" in b2.summary()
    with pytest.raises(KeyError):
        b2.update("RU000A000000", status="hold")
    with pytest.raises(ValueError):
        b2.add("RU000A000001", "X", "X", "C", 1)


def _cfg(tmp_path):
    cfg = tmp_path / "c.yaml"
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg.write_text(f"""
data:
  basket_csv: {tmp_path / 'data' / 'basket.csv'}
  ratings_csv: ''
execution:
  broker: paper
  state_path: {tmp_path / 'pf.json'}
  journal_path: {tmp_path / 'o.jsonl'}
""", encoding="utf-8")
    return cfg


def test_basket_cli_offline_and_live(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert main(["-c", str(cfg), "basket"]) == 0
    assert "Корзина пуста" in capsys.readouterr().out
    # add без сети (--isin/--name), затем через поиск на фикстурах
    assert main(["-c", str(cfg), "basket", "add", "--isin", "RU000A103WV8", "--name", "ВИС Ф БП04", "--list", "B", "--weight", "20",
                 "--note", "тест", "--date", "2026-09-14"]) == 0
    assert main(["-c", str(cfg), "--fixtures", FIX, "basket", "add", "МТС 1P", "--list", "A", "--weight", "15"]) == 0
    out = capsys.readouterr().out
    assert "add B 20%" in out and "МТС 1P-21" in out
    with pytest.raises(SystemExit):
        main(["-c", str(cfg), "--fixtures", FIX, "basket", "add", "ОФЗ"])   # несколько совпадений
    assert main(["-c", str(cfg), "basket", "set", "вис", "--status", "hold", "--note", "дефолт"]) == 0
    assert main(["-c", str(cfg), "basket", "list", "--all"]) == 0
    assert main(["-c", str(cfg), "basket", "log"]) == 0
    out = capsys.readouterr().out
    assert "пауза" in out and "в портфеле 1" in out and "status в портфеле → пауза — дефолт" in out
    # живой срез на фикстурах: ВИС вне сита (без рейтинга, дефолтные новости в фикстурном скрине нет — только флаг сита)
    assert main(["-c", str(cfg), "--fixtures", FIX, "basket", "status"]) == 0
    out = capsys.readouterr().out
    assert "МТС 1P-21" in out and "ВИС Ф БП04" in out and "Список A" in out


def test_basket_rows_and_report_sections(tmp_path):
    from bondtrader.bot import keyboard, render
    from bondtrader.cli import build_parser, collect_report
    from bondtrader.config import Settings
    from bondtrader.report_tg import section_basket
    cfg = _cfg(tmp_path)
    (tmp_path / "pf.json").write_text(json.dumps({"cash": 1000000, "positions": {}, "realized_pnl": 0, "coupons_received": 0, "commissions_paid": 0}), encoding="utf-8")
    book = BasketBook([], str(tmp_path / "data" / "basket.csv"))
    book.add("RU000A104ZK2", "RU000A104ZK2", "МТС 1P-21", "A", 60, on=date(2026, 9, 14))
    book.add("RU000A103WV8", "RU000A103WV8", "ВИС Ф БП04", "B", 50, on=date(2026, 9, 14))
    book.add("RU000A105XX1", "RU000A105XX1", "Сегежа3P4R", "B", 50, status="wait", on=date(2026, 9, 14))
    book.add("XX0000000000", "XX0000000000", "Нет такой", "B", 0, on=date(2026, 9, 14))
    book.save()
    args = build_parser().parse_args(["-c", str(cfg), "--fixtures", FIX, "report", "-s", "carry"])
    d = collect_report(args, Settings.load(str(cfg)))
    rows = {br.entry.isin: br for br in d["basket"]}
    assert set(rows) == {"RU000A104ZK2", "RU000A103WV8", "RU000A105XX1", "XX0000000000"}
    assert rows["XX0000000000"].row is None and rows["XX0000000000"].flags == ["нет на MOEX / нет цены"]
    assert all(br.row is not None for isin, br in rows.items() if isin != "XX0000000000")
    assert any("нет рейтинга" in f for f in rows["RU000A104ZK2"].flags)      # книга рейтингов пустая
    sec = "\n".join(section_basket(d))
    assert "<b>Корзина</b>" in sec and "МТС 1P-21" in sec and "Список A" in sec and "Вне портфеля" in sec and "Сегежа3P4R" in sec
    assert "⚠️ <b>Нет такой</b>" in sec
    tg = render("basket", d)
    assert "<b>Корзина</b>" in tg and "<b>Цель" not in tg
    assert "basket" in [b["callback_data"] for row in keyboard()["inline_keyboard"] for b in row]
    assert "## Корзина" in __import__("bondtrader.cli", fromlist=["build_report"]).build_report(args, Settings.load(str(cfg)), d)
    # пустая книга — секции нет
    empty = dict(d, basket_book=BasketBook(), basket=[])
    assert section_basket(empty) == [] and "пуста" in render("basket", empty)
    assert basket_rows(BasketBook(), d["snap"], d["rows"]) == []

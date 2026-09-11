import json
import os
from datetime import date

import pytest

from bondtrader.data.moex import (
    apply_bondization, parse_board_securities, parse_bondization, parse_index_history,
)
from bondtrader.data.cbr import analyze_keyrate, parse_keyrate_xml

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def test_parse_board_securities():
    pairs = parse_board_securities(load("bonds_board.json"), date(2025, 6, 2))
    assert len(pairs) == 20
    by = {b.secid: (b, q) for b, q in pairs}
    b, q = by["SU26238RMFS4"]
    assert b.is_ofz and b.maturity == date(2041, 5, 15) and b.coupon_period == 182
    assert q.price and 40 < q.price < 70 and q.ytm_moex and 10 < q.ytm_moex < 25
    assert q.duration_moex and 6 < q.duration_moex < 9
    assert q.bid == pytest.approx(q.price - 0.05) and q.ask == pytest.approx(q.price + 0.05) and q.turnover == 900e6
    assert q.bid_ask_spread_pct == pytest.approx(0.1 / q.bid * 100)
    mts, _ = by["RU000A104ZK2"]
    assert mts.offer_date == date(2026, 9, 1) and mts.buyback_price == 100
    assert by["SU29014RMFS6"][0].is_floater and by["SU52002RMFS1"][0].is_linker
    assert by["XS0000000001"][1].price is None
    amort, _ = by["RU000A100AMR"]
    assert amort.has_amortization and amort.face_value == 500
    assert by["RU000A100XY9"][0].currency == "USD"


def test_parse_bondization_and_apply():
    sched = parse_bondization(load("bondization_mts.json"))
    assert sched["coupons"][0] == (date(2025, 9, 1), 49.86)
    assert sched["coupons"][3][1] is None
    assert sched["offers"][0]["date"] == date(2026, 9, 1)
    from bondtrader.models import Bond
    b = Bond(secid="RU000A104ZK2", name="МТС 1P-21", maturity=date(2029, 3, 1), coupon_value=49.86, coupon_period=182,
             next_coupon=date(2025, 9, 1))
    apply_bondization(b, sched, today=date(2025, 6, 2))
    assert b.has_full_schedule and b.offer_date == date(2026, 9, 1)
    # купоны после оферты неизвестны, но известных больше — это не флоатер
    assert not b.is_floater

    sched2 = parse_bondization(load("bondization_amort.json"))
    assert [a[1] for a in sched2["amortizations"]] == [500.0, 250.0, 250.0]


def test_parse_index_history():
    rows = parse_index_history(load("index_history_page1.json"))
    assert len(rows) == 100
    assert rows[0][0] == date(2024, 1, 9) and rows[0][1] > 0


def test_keyrate_parse_and_regime():
    with open(os.path.join(FIX, "cbr_keyrate.xml"), encoding="utf-8") as f:
        hist = parse_keyrate_xml(f.read())
    assert [r for _, r in hist] == [18.0, 19.0, 21.0, 21.0, 20.0]
    view = analyze_keyrate(hist)
    assert view.current == 20.0 and view.regime == "easing" and view.last_change == -1.0
    assert view.consecutive_moves == 1 and view.peak == 21.0
    # если последнее изменение давно — hold
    old = [(date(2024, 1, 1), 16.0), (date(2024, 2, 1), 16.0), (date(2025, 6, 1), 16.0)]
    assert analyze_keyrate(old).regime == "hold"
    tight = [(date(2025, 1, 1), 16.0), (date(2025, 2, 1), 18.0), (date(2025, 3, 1), 19.0), (date(2025, 3, 15), 19.0)]
    v2 = analyze_keyrate(tight)
    assert v2.regime == "tightening" and v2.consecutive_moves == 2


def test_face_currency_wins_over_settlement_currency():
    from bondtrader.data.moex import parse_bond_row
    b = parse_bond_row({"SECID": "X", "SHORTNAME": "ПолиплП2Б3", "FACEUNIT": "USD", "CURRENCYID": "SUR", "FACEVALUE": "1000", "MATDATE": "2027-01-01"})
    assert b.currency == "USD"
    b = parse_bond_row({"SECID": "Y", "SHORTNAME": "Обычная", "FACEUNIT": "SUR", "CURRENCYID": "SUR", "FACEVALUE": "1000", "MATDATE": "2027-01-01"})
    assert b.currency == "SUR"

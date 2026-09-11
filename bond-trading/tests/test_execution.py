import json
import os
from datetime import date

import pytest

from bondtrader.analytics.curve import ZeroCurve
from bondtrader.data.moex import parse_board_securities
from bondtrader.execution.executor import Executor
from bondtrader.execution.paper import PaperBroker
from bondtrader.execution.tinvest import TInvestBroker, from_quotation, to_quotation
from bondtrader.portfolio import Order, Portfolio
from bondtrader.risk import RiskLimits, RiskManager
from bondtrader.screener import Screener, ScreenerConfig

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SETTLE = date(2025, 6, 2)


@pytest.fixture
def rows():
    with open(os.path.join(FIX, "bonds_board.json"), encoding="utf-8") as f:
        u = parse_board_securities(json.load(f), SETTLE)
    with open(os.path.join(FIX, "zcyc.json")) as f:
        c = ZeroCurve.from_moex_zcyc(json.load(f))
    return {r.secid: r for r in Screener(ScreenerConfig()).run(u, c, SETTLE)}


def test_quotation_roundtrip():
    q = to_quotation(98.7654321)
    assert q == {"units": "98", "nano": 765432100}
    assert from_quotation(q) == pytest.approx(98.7654321)
    assert from_quotation({"units": "-1", "nano": 0}) == -1.0
    assert from_quotation(None) == 0.0


def test_paper_broker_and_executor(tmp_path, rows):
    state = tmp_path / "pf.json"
    journal = tmp_path / "orders.jsonl"
    broker = PaperBroker(str(state), initial_cash=500_000, commission_bp=5)
    ex = Executor(broker, RiskManager(RiskLimits()), str(journal), dry_run=True)
    secid = "SU26238RMFS4"
    orders = [Order(secid, "BUY", 100, rows[secid].metrics.clean_price, "тест", "manual")]
    rep = ex.run(orders, rows, broker.portfolio)
    assert not rep.aborted and rep.reports[0].status == "dry_run" and broker.positions() == {}
    ex.dry_run = False
    rep = ex.run(orders, rows, broker.portfolio)
    assert rep.filled == 1 and broker.positions() == {secid: 100}
    assert broker.cash() < 500_000
    # состояние сохранилось, журнал ведётся
    reloaded = PaperBroker(str(state))
    assert reloaded.positions() == {secid: 100}
    lines = journal.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[-1])["status"] == "filled"
    # жёсткое нарушение (нет денег) -> abort
    huge = [Order(secid, "BUY", 100_000, rows[secid].metrics.clean_price)]
    rep = ex.run(huge, rows, broker.portfolio)
    assert rep.aborted and any(v.code == "cash" for v in rep.violations)
    # продажа без позиции отклоняется брокером
    rep = ex.run([Order("SU26207RMFS9", "SELL", 1, 90.0)], rows, Portfolio(cash=0))
    assert rep.aborted


def test_tinvest_broker_with_fake_transport(rows):
    calls = []

    def fake_post(method, body):
        calls.append((method, body))
        if method == "UsersService/GetAccounts":
            return {"accounts": [{"id": "acc-1", "status": "ACCOUNT_STATUS_OPEN"}]}
        if method == "InstrumentsService/BondBy":
            assert body["id_type"] == "INSTRUMENT_ID_TYPE_TICKER"
            if body["id"] != "SU26238RMFS4" or body["class_code"] != "TQOB":
                raise RuntimeError("HTTP 404: not found")
            return {"instrument": {"uid": "uid-238", "figi": "BBG00", "ticker": "SU26238RMFS4", "lot": 1}}
        if method == "MarketDataService/GetTradingStatus":
            assert body["instrument_id"] == "uid-238"
            return {"trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING", "api_trade_available_flag": True}
        if method == "InstrumentsService/FindInstrument":
            return {"instruments": [{"uid": "uid-x", "isin": body["query"], "ticker": "RU000A1XXXXX", "lot": 1}]}
        if method == "OrdersService/PostOrder":
            assert body["instrument_id"] == "uid-238" and body["quantity"] == "10" and body["account_id"] == "acc-1"
            assert body["order_type"] in ("ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET")
            if body["order_type"] == "ORDER_TYPE_LIMIT":
                assert body["price"]["units"] == "53"
            return {"order_id": "ord-1", "execution_report_status": "EXECUTION_REPORT_STATUS_FILL", "lots_executed": 10,
                    "executed_order_price": {"units": "532", "nano": 0}, "executed_commission": {"units": "3", "nano": 0}}
        if method == "OperationsService/GetPositions":
            return {"money": [{"currency": "rub", "units": "100000", "nano": 500000000}, {"currency": "usd", "units": "5", "nano": 0}]}
        if method == "OperationsService/GetPortfolio":
            return {"positions": [{"instrument_type": "bond", "instrument_uid": "uid-238", "quantity": {"units": "10", "nano": 0},
                                   "average_position_price": {"units": "531", "nano": 500000000}},
                                  {"instrumentType": "share", "instrumentUid": "x", "quantity": {"units": "1", "nano": 0}}]}
        if method == "InstrumentsService/GetInstrumentBy":
            return {"instrument": {"uid": "uid-238", "ticker": "SU26238RMFS4", "nominal": {"units": "1000", "nano": 0, "currency": "rub"}}}
        if method == "OrdersService/GetOrders":
            return {"orders": [{"order_id": "o1"}]}
        if method == "OrdersService/CancelOrder":
            assert body["order_id"] == "o1"
            return {}
        if method == "SandboxService/SandboxPayIn":
            # на счёте 100000.5 -> доводим до 500000
            assert body["amount"]["units"] == "399999"
            return {}
        raise AssertionError(method)

    br = TInvestBroker(token="", sandbox=True, post=fake_post, order_type="limit")
    assert br.account_id() == "acc-1"
    row = rows["SU26238RMFS4"]
    rep = br.place_order(Order("SU26238RMFS4", "BUY", 10, row.metrics.clean_price), row.bond, row.quote)
    assert rep.status == "filled" and rep.filled_qty == 10 and rep.fill_price == pytest.approx(53.2) and rep.commission == 3.0
    assert br.cash() == pytest.approx(100000.5)
    assert br.positions() == {"SU26238RMFS4": 10}
    assert br.positions_detailed() == {"SU26238RMFS4": (10, pytest.approx(53.15))}   # рубли -> % от номинала
    assert br.cancel_all() == 1
    # лимитная цена: покупка по аску с запасом, продажа по биду; без стакана — последняя цена
    from bondtrader.models import Quote as _Q
    q = _Q("X", row.quote.trade_date, price=100.0, bid=99.5, ask=100.5)
    assert TInvestBroker.limit_price(Order("X", "BUY", 1, 100.0), q) == pytest.approx(100.65)
    assert TInvestBroker.limit_price(Order("X", "SELL", 1, 100.0), q) == pytest.approx(99.35)
    assert TInvestBroker.limit_price(Order("X", "BUY", 1, 100.0), _Q("X", row.quote.trade_date, price=100.0)) == pytest.approx(100.15)
    # песочница по умолчанию — рыночные заявки (лимитные там не сводятся), бой — лимитные
    assert TInvestBroker(token="", sandbox=True, post=fake_post).order_type == "market"
    assert TInvestBroker(token="", sandbox=False, post=fake_post).order_type == "limit"
    mk = TInvestBroker(token="", sandbox=True, post=fake_post)
    calls.clear()
    mk.place_order(Order("SU26238RMFS4", "BUY", 10, row.metrics.clean_price), row.bond, row.quote)
    post = next(b for m, b in calls if m == "OrdersService/PostOrder")
    assert post["order_type"] == "ORDER_TYPE_MARKET" and "price" not in post
    # запасной путь: бумага не найдена по тикеру -> FindInstrument по ISIN
    from bondtrader.models import Bond as _B
    other = _B(secid="RU000A1XXXXX", isin="RU000A1XXXXX", board="TQCB")
    calls.clear()
    assert br.instrument(other)["uid"] == "uid-x"
    assert any(m == "InstrumentsService/FindInstrument" for m, _ in calls)
    # sandbox_open переиспользует открытый счёт и не вызывает OpenSandboxAccount
    assert br.sandbox_open(500_000) == "acc-1"
    assert not any(m == "SandboxService/OpenSandboxAccount" for m, _ in calls)
    with pytest.raises(ValueError):
        TInvestBroker(token="")

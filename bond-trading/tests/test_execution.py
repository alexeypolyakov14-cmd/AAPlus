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
            return {"instrument": {"uid": "uid-238", "figi": "BBG00", "ticker": "SU26238RMFS4", "lot": 1}}
        if method == "OrdersService/PostOrder":
            assert body["instrumentId"] == "uid-238" and body["quantity"] == "10"
            assert body["orderType"] == "ORDER_TYPE_LIMIT" and body["price"]["units"] == "53"
            return {"orderId": "ord-1", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": 10,
                    "executedOrderPrice": {"units": "53", "nano": 200000000}, "executedCommission": {"units": "3", "nano": 0}}
        if method == "OperationsService/GetPositions":
            return {"money": [{"currency": "rub", "units": "100000", "nano": 500000000}, {"currency": "usd", "units": "5", "nano": 0}]}
        if method == "OperationsService/GetPortfolio":
            return {"positions": [{"instrumentType": "bond", "instrumentUid": "uid-238", "quantity": {"units": "10", "nano": 0}},
                                  {"instrumentType": "share", "instrumentUid": "x", "quantity": {"units": "1", "nano": 0}}]}
        if method == "InstrumentsService/GetInstrumentBy":
            return {"instrument": {"uid": "uid-238", "ticker": "SU26238RMFS4"}}
        if method == "OrdersService/GetOrders":
            return {"orders": [{"orderId": "o1"}]}
        if method == "OrdersService/CancelOrder":
            return {}
        raise AssertionError(method)

    br = TInvestBroker(token="", sandbox=True, post=fake_post)
    assert br.account_id() == "acc-1"
    row = rows["SU26238RMFS4"]
    rep = br.place_order(Order("SU26238RMFS4", "BUY", 10, row.metrics.clean_price), row.bond, row.quote)
    assert rep.status == "filled" and rep.filled_qty == 10 and rep.fill_price == pytest.approx(53.2) and rep.commission == 3.0
    assert br.cash() == pytest.approx(100000.5)
    assert br.positions() == {"SU26238RMFS4": 10}
    assert br.cancel_all() == 1
    with pytest.raises(ValueError):
        TInvestBroker(token="")

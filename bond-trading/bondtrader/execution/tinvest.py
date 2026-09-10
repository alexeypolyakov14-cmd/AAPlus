"""Брокер T-Invest API (Т-Банк/Тинькофф Инвестиции) через REST-шлюз.

Документация: https://developer.tbank.ru/invest/intro/intro
Токен — переменная окружения (по умолчанию TINVEST_TOKEN). Для песочницы используется
отдельный хост; по умолчанию sandbox=True. Боевой режим включается явно.

Цены облигаций в API задаются в процентах от номинала (Quotation units/nano),
количество — в лотах (у облигаций лот, как правило, 1 бумага).

TLS: хосты *.tinkoff.ru используют сертификаты УЦ Минцифры. Если их нет в системном
хранилище, укажите путь к бандлу (certifi + Russian Trusted Root/Sub CA) в переменной
TINVEST_CA_BUNDLE или в config.yaml -> execution.tinvest.ca_bundle.
"""
from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

import requests

from ..models import Bond, Quote
from ..portfolio import Order
from .base import OrderReport

log = logging.getLogger(__name__)

PROD_URL = "https://invest-public-api.tinkoff.ru/rest/"
SANDBOX_URL = "https://sandbox-invest-public-api.tinkoff.ru/rest/"
SVC = "tinkoff.public.invest.api.contract.v1."


# ---- Quotation helpers ----
def to_quotation(value: float) -> dict:
    q = Decimal(str(value)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    units = int(q)
    nano = int((q - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def from_quotation(q: Optional[dict]) -> float:
    if not q:
        return 0.0
    return float(int(q.get("units", 0) or 0)) + float(q.get("nano", 0) or 0) / 1e9


def order_status_text(status: str) -> str:
    return {
        "EXECUTION_REPORT_STATUS_FILL": "filled",
        "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "accepted",
        "EXECUTION_REPORT_STATUS_NEW": "accepted",
        "EXECUTION_REPORT_STATUS_REJECTED": "rejected",
        "EXECUTION_REPORT_STATUS_CANCELLED": "rejected",
    }.get(status, "accepted")


class TInvestBroker:
    name = "tinvest"

    def __init__(self, token: str, sandbox: bool = True, account_id: str = "", timeout: float = 15,
                 post: Optional[Callable[[str, dict], dict]] = None, ca_bundle: Optional[str] = None):
        if not token and post is None:
            raise ValueError("не задан токен T-Invest API (переменная окружения TINVEST_TOKEN)")
        self.token = token
        self.verify: str | bool = ca_bundle or os.environ.get("TINVEST_CA_BUNDLE") or True
        self.sandbox = sandbox
        self.base = SANDBOX_URL if sandbox else PROD_URL
        self.timeout = timeout
        self._account = account_id
        self._post = post or self._http_post
        self._by_secid: dict[str, dict] = {}   # secid -> instrument
        self._by_uid: dict[str, dict] = {}

    # ---- транспорт ----
    def _http_post(self, method: str, body: dict) -> dict:
        url = self.base + SVC + method
        try:
            resp = requests.post(url, json=body, timeout=self.timeout, verify=self.verify,
                                 headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                          "x-app-name": "bondtrader"})
        except requests.exceptions.SSLError as e:
            raise RuntimeError("T-Invest: ошибка TLS. Хосты tinkoff.ru используют сертификаты УЦ Минцифры — "
                               "укажите бандл в TINVEST_CA_BUNDLE (см. README). " + str(e)[:200]) from e
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"message": resp.text}
            raise RuntimeError(f"T-Invest {method}: HTTP {resp.status_code}: {err.get('message') or err}")
        return resp.json() if resp.text else {}

    def call(self, method: str, body: Optional[dict] = None) -> dict:
        return self._post(method, body or {})

    # ---- счета ----
    def account_id(self) -> str:
        if self._account:
            return self._account
        accs = self.call("UsersService/GetAccounts").get("accounts", [])
        open_accs = [a for a in accs if a.get("status") in (None, "ACCOUNT_STATUS_OPEN")]
        if not open_accs:
            raise RuntimeError("нет открытых счетов; для песочницы выполните sandbox_open()")
        self._account = open_accs[0]["id"]
        return self._account

    def sandbox_open(self, pay_in_rub: float = 0.0, reuse: bool = True) -> str:
        """Открывает счёт песочницы (или переиспользует уже открытый) и пополняет его."""
        if not self.sandbox:
            raise RuntimeError("sandbox_open доступен только в режиме песочницы")
        acc = ""
        if reuse:
            accs = self.call("UsersService/GetAccounts").get("accounts", [])
            open_accs = [a for a in accs if a.get("status") in (None, "ACCOUNT_STATUS_OPEN")]
            if open_accs:
                acc = open_accs[0]["id"]
        if not acc:
            acc = self.call("SandboxService/OpenSandboxAccount", {"name": "bondtrader"}).get("accountId", "")
        self._account = acc
        if pay_in_rub > 0:
            self.call("SandboxService/SandboxPayIn", {"accountId": acc, "amount": {"currency": "rub", **to_quotation(pay_in_rub)}})
        return acc

    # ---- инструменты ----
    def instrument(self, bond: Bond) -> dict:
        if bond.secid in self._by_secid:
            return self._by_secid[bond.secid]
        body = {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": bond.board or "TQOB", "id": bond.secid}
        if bond.isin:
            body = {"idType": "INSTRUMENT_ID_TYPE_ISIN", "id": bond.isin}
        inst = self.call("InstrumentsService/BondBy", body).get("instrument") or {}
        if not inst:
            raise RuntimeError(f"{bond.secid}: инструмент не найден в T-Invest")
        self._by_secid[bond.secid] = inst
        self._by_uid[inst.get("uid", "")] = inst
        return inst

    def _instrument_by_uid(self, uid: str) -> dict:
        if uid in self._by_uid:
            return self._by_uid[uid]
        inst = self.call("InstrumentsService/GetInstrumentBy", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}).get("instrument") or {}
        self._by_uid[uid] = inst
        if inst.get("ticker"):
            self._by_secid[inst["ticker"]] = inst
        return inst

    # ---- состояние счёта ----
    def cash(self) -> float:
        pos = self.call("OperationsService/GetPositions", {"accountId": self.account_id()})
        total = 0.0
        for m in pos.get("money", []):
            if (m.get("currency") or "").lower() == "rub":
                total += from_quotation(m)
        return total

    def positions(self) -> dict[str, int]:
        pf = self.call("OperationsService/GetPortfolio", {"accountId": self.account_id(), "currency": "RUB"})
        out: dict[str, int] = {}
        for p in pf.get("positions", []):
            if p.get("instrumentType") != "bond":
                continue
            uid = p.get("instrumentUid") or ""
            inst = self._instrument_by_uid(uid) if uid else {}
            ticker = inst.get("ticker") or p.get("figi") or uid
            qty = int(round(from_quotation(p.get("quantity"))))
            if qty:
                out[ticker] = out.get(ticker, 0) + qty
        return out

    def last_price(self, bond: Bond) -> Optional[float]:
        inst = self.instrument(bond)
        r = self.call("MarketDataService/GetLastPrices", {"instrumentId": [inst["uid"]]})
        for lp in r.get("lastPrices", []):
            return from_quotation(lp.get("price"))
        return None

    # ---- ордера ----
    def place_order(self, order: Order, bond: Bond, quote: Quote) -> OrderReport:
        inst = self.instrument(bond)
        lot = int(inst.get("lot") or 1)
        lots = max(order.qty // lot, 0)
        if lots == 0:
            return OrderReport(order, "rejected", message=f"количество {order.qty} меньше лота {lot}")
        body: dict[str, Any] = {
            "instrumentId": inst["uid"],
            "quantity": str(lots),
            "direction": "ORDER_DIRECTION_BUY" if order.side == "BUY" else "ORDER_DIRECTION_SELL",
            "accountId": self.account_id(),
            "orderId": str(uuid.uuid4()),
        }
        if order.price:
            body["orderType"] = "ORDER_TYPE_LIMIT"
            body["price"] = to_quotation(round(order.price, 4))
        else:
            body["orderType"] = "ORDER_TYPE_BESTPRICE"
        try:
            r = self.call("OrdersService/PostOrder", body)
        except RuntimeError as e:
            return OrderReport(order, "rejected", message=str(e))
        status = order_status_text(r.get("executionReportStatus", ""))
        filled_lots = int(r.get("lotsExecuted", 0) or 0)
        avg = from_quotation(r.get("executedOrderPrice")) if r.get("executedOrderPrice") else None
        commission = from_quotation(r.get("executedCommission")) if r.get("executedCommission") else 0.0
        return OrderReport(order, status, broker_order_id=r.get("orderId", ""), filled_qty=filled_lots * lot,
                           fill_price=avg if avg else None, commission=commission, message=r.get("message", ""))

    def open_orders(self) -> list[dict]:
        return self.call("OrdersService/GetOrders", {"accountId": self.account_id()}).get("orders", [])

    def cancel_all(self) -> int:
        n = 0
        for o in self.open_orders():
            self.call("OrdersService/CancelOrder", {"accountId": self.account_id(), "orderId": o.get("orderId")})
            n += 1
        return n

"""Бумажный брокер: мгновенное исполнение по цене котировки, состояние в JSON."""
from __future__ import annotations

from datetime import date
from typing import Optional

from ..models import Bond, Quote
from ..portfolio import Fill, Order, Portfolio
from .base import OrderReport


class PaperBroker:
    name = "paper"

    def __init__(self, state_path: str = "state/portfolio.json", initial_cash: float = 1_000_000,
                 commission_bp: float = 5, slippage_bp: float = 0):
        self.state_path = state_path
        self.portfolio = Portfolio.load(state_path, default_cash=initial_cash)
        self.commission = commission_bp / 10000
        self.slippage = slippage_bp / 10000

    def account_id(self) -> str:
        return "paper"

    def cash(self) -> float:
        return self.portfolio.cash

    def positions(self) -> dict[str, int]:
        return {s: p.qty for s, p in self.portfolio.positions.items()}

    def place_order(self, order: Order, bond: Bond, quote: Quote) -> OrderReport:
        ref = order.price or quote.price
        if ref is None:
            return OrderReport(order, "rejected", message="нет цены")
        px = ref * (1 + self.slippage if order.side == "BUY" else 1 - self.slippage)
        gross = order.qty * (bond.face_value * px / 100 + quote.accrued)
        fee = gross * self.commission
        if order.side == "BUY" and gross + fee > self.portfolio.cash + 1e-6:
            return OrderReport(order, "rejected", message=f"недостаточно денег: нужно {gross + fee:,.0f}, есть {self.portfolio.cash:,.0f}")
        try:
            self.portfolio.apply_fill(Fill(order.secid, order.side, order.qty, px, quote.accrued, bond.face_value, fee, date.today()))
        except ValueError as e:
            return OrderReport(order, "rejected", message=str(e))
        self.portfolio.save(self.state_path)
        return OrderReport(order, "filled", broker_order_id=f"paper-{order.secid}-{order.side}", filled_qty=order.qty,
                           fill_price=px, commission=fee)

    def open_orders(self) -> list[dict]:
        return []

    def cancel_all(self) -> int:
        return 0

    def reset(self, cash: float) -> None:
        self.portfolio = Portfolio(cash=cash)
        self.portfolio.save(self.state_path)

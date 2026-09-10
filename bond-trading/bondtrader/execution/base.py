"""Интерфейс брокера и отчёт об исполнении."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Protocol

from ..models import Bond, Quote
from ..portfolio import Order


@dataclass
class OrderReport:
    order: Order
    status: str                 # "filled" | "accepted" | "rejected" | "dry_run"
    broker_order_id: str = ""
    filled_qty: int = 0
    fill_price: Optional[float] = None
    commission: float = 0.0
    message: str = ""
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["order"] = self.order.as_dict()
        return d


class Broker(Protocol):
    name: str

    def account_id(self) -> str: ...
    def cash(self) -> float: ...
    def positions(self) -> dict[str, int]: ...
    def place_order(self, order: Order, bond: Bond, quote: Quote) -> OrderReport: ...
    def open_orders(self) -> list[dict]: ...
    def cancel_all(self) -> int: ...

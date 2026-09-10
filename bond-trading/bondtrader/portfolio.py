"""Портфель облигаций: позиции, денежные средства, оценка, учёт купонов и погашений."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


@dataclass
class Position:
    secid: str
    qty: int
    avg_price: float           # средняя чистая цена, % от номинала
    face: float = 1000.0       # номинал на момент покупки (для отчётности)

    def cost(self) -> float:
        return self.qty * self.face * self.avg_price / 100


@dataclass
class Order:
    secid: str
    side: str                  # "BUY" | "SELL"
    qty: int
    price: Optional[float] = None   # лимитная чистая цена, % от номинала; None — рыночная
    reason: str = ""
    strategy: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fill:
    secid: str
    side: str
    qty: int
    price: float               # чистая цена, %
    accrued: float             # НКД руб./бумага
    face: float
    commission: float = 0.0
    trade_date: Optional[date] = None


@dataclass
class Portfolio:
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    coupons_received: float = 0.0
    commissions_paid: float = 0.0

    # ---- сделки ----
    def apply_fill(self, f: Fill) -> None:
        gross = f.qty * (f.face * f.price / 100 + f.accrued)
        pos = self.positions.get(f.secid)
        if f.side == "BUY":
            self.cash -= gross + f.commission
            if pos:
                total_cost = pos.cost() + f.qty * f.face * f.price / 100
                pos.qty += f.qty
                pos.avg_price = total_cost / (pos.qty * f.face) * 100
                pos.face = f.face
            else:
                self.positions[f.secid] = Position(f.secid, f.qty, f.price, f.face)
        elif f.side == "SELL":
            if not pos or pos.qty < f.qty:
                raise ValueError(f"недостаточно бумаг {f.secid} для продажи {f.qty}")
            self.cash += gross - f.commission
            self.realized_pnl += f.qty * f.face * (f.price - pos.avg_price) / 100
            pos.qty -= f.qty
            if pos.qty == 0:
                del self.positions[f.secid]
        else:
            raise ValueError(f"неизвестная сторона {f.side}")
        self.commissions_paid += f.commission

    def apply_coupon(self, secid: str, per_bond: float) -> float:
        pos = self.positions.get(secid)
        if not pos or per_bond <= 0:
            return 0.0
        amount = pos.qty * per_bond
        self.cash += amount
        self.coupons_received += amount
        return amount

    def apply_redemption(self, secid: str, per_bond: float, full: bool) -> float:
        """Погашение (полное или амортизационное) на сумму per_bond руб. на бумагу."""
        pos = self.positions.get(secid)
        if not pos or per_bond <= 0:
            return 0.0
        amount = pos.qty * per_bond
        self.cash += amount
        self.realized_pnl += pos.qty * per_bond * (1 - pos.avg_price / 100)
        if full:
            del self.positions[secid]
        else:
            pos.face = max(pos.face - per_bond, 0.0)
        return amount

    # ---- оценка ----
    def position_value(self, secid: str, price: float, accrued: float, face: float) -> float:
        pos = self.positions.get(secid)
        if not pos:
            return 0.0
        return pos.qty * (face * price / 100 + accrued)

    def nav(self, marks: dict[str, tuple[float, float, float]]) -> float:
        """marks: secid -> (чистая цена %, НКД руб., номинал руб.). Позиции без котировки оцениваются по средней цене."""
        total = self.cash
        for secid, pos in self.positions.items():
            if secid in marks:
                p, a, f = marks[secid]
                total += pos.qty * (f * p / 100 + a)
            else:
                total += pos.cost()
        return total

    def weights(self, marks: dict[str, tuple[float, float, float]]) -> dict[str, float]:
        nav = self.nav(marks)
        if nav <= 0:
            return {}
        out = {}
        for secid, pos in self.positions.items():
            if secid in marks:
                p, a, f = marks[secid]
                out[secid] = pos.qty * (f * p / 100 + a) / nav
            else:
                out[secid] = pos.cost() / nav
        return out

    # ---- сериализация ----
    def to_dict(self) -> dict:
        return {
            "cash": self.cash, "realized_pnl": self.realized_pnl, "coupons_received": self.coupons_received,
            "commissions_paid": self.commissions_paid,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        p = cls(cash=d.get("cash", 0.0), realized_pnl=d.get("realized_pnl", 0.0),
                coupons_received=d.get("coupons_received", 0.0), commissions_paid=d.get("commissions_paid", 0.0))
        for k, v in (d.get("positions") or {}).items():
            p.positions[k] = Position(**v)
        return p

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str, default_cash: float = 0.0) -> "Portfolio":
        if not os.path.exists(path):
            return cls(cash=default_cash)
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

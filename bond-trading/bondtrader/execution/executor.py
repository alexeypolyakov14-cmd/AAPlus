"""Исполнитель: риск-проверка -> (dry-run | отправка брокеру) -> журнал."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from ..portfolio import Order, Portfolio
from ..risk import RiskManager, Violation
from ..screener import ScreenRow
from .base import Broker, OrderReport


@dataclass
class ExecutionReport:
    reports: list[OrderReport] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    aborted: bool = False

    @property
    def filled(self) -> int:
        return sum(1 for r in self.reports if r.status == "filled")

    @property
    def rejected(self) -> int:
        return sum(1 for r in self.reports if r.status == "rejected")


class Executor:
    def __init__(self, broker: Broker, risk: RiskManager, journal_path: str = "state/orders.jsonl", dry_run: bool = True):
        self.broker = broker
        self.risk = risk
        self.journal_path = journal_path
        self.dry_run = dry_run

    def _journal(self, rec: dict) -> None:
        os.makedirs(os.path.dirname(self.journal_path) or ".", exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "broker": self.broker.name, "dry_run": self.dry_run, **rec}
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def run(self, orders: list[Order], rows: dict[str, ScreenRow], portfolio: Portfolio) -> ExecutionReport:
        rep = ExecutionReport()
        marks = {s: (r.metrics.clean_price, r.quote.accrued, r.bond.face_value) for s, r in rows.items()}
        nav = portfolio.nav(marks)
        rep.violations = self.risk.check_orders(orders, rows, portfolio, nav)
        if any(v.hard for v in rep.violations):
            rep.aborted = True
            self._journal({"event": "abort", "violations": [v.message for v in rep.violations if v.hard]})
            return rep
        for o in orders:
            row = rows[o.secid]
            if self.dry_run:
                r = OrderReport(o, "dry_run", message="не отправлен (dry-run)")
            else:
                r = self.broker.place_order(o, row.bond, row.quote)
            rep.reports.append(r)
            self._journal({"event": "order", **r.as_dict()})
        return rep

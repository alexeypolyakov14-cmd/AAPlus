"""Корзина — накопительная книга выбранных бумаг.

Скрин и стратегии пересчитываются каждый день с нуля, поэтому их списки «дёргаются»: бумага у порога оборота
входит и выходит, книга рейтингов перезагружается, внутри эмитента выбирается другой выпуск. Корзина — наоборот,
ручной список: бумага попадает в неё решением пользователя и остаётся навсегда. Меняется только статус
(active — в портфеле, hold — приостановлена до выяснения, wait — лист ожидания, removed — исключена), вес и
заметка, и каждое изменение пишется в журнал с датой и причиной. Книга живёт в репозитории
(data/books/basket.csv + basket_log.csv), а не в кэше Actions, и правится только явными командами
``bondtrader basket add|set``. Ежедневный отчёт и бот показывают её живой срез: цены, YTW, спред, премию к пирам,
что изменилось за 30 дней и какие флаги (вне сита, дефолт в реестре MOEX, нет рейтинга, негатив в новостях).
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

STATUSES = ("active", "hold", "wait", "removed")
LISTS = ("A", "B")
STATUS_RU = {"active": "в портфеле", "hold": "пауза", "wait": "лист ожидания", "removed": "исключена"}
FIELDS = ("isin", "secid", "name", "list", "weight", "status", "added", "changed", "note")
LOG_FIELDS = ("date", "isin", "name", "action", "detail", "note")


@dataclass
class BasketEntry:
    isin: str
    secid: str
    name: str
    list: str                 # A (ступень A-, с плечом) | B (ВДО, без плеча)
    weight: float             # доля капитала своего списка, %
    status: str               # active | hold | wait | removed
    added: date
    changed: date
    note: str = ""

    @property
    def live(self) -> bool:
        """Показывать в отчёте (всё, кроме исключённых)."""
        return self.status != "removed"

    def as_dict(self) -> dict:
        return {"isin": self.isin, "secid": self.secid, "name": self.name, "list": self.list, "weight": f"{self.weight:g}",
                "status": self.status, "added": self.added.isoformat(), "changed": self.changed.isoformat(), "note": self.note}


@dataclass
class BasketEvent:
    date: date
    isin: str
    name: str
    action: str               # add | status | weight | list | note
    detail: str               # что именно: «hold → active», «10 → 12»
    note: str = ""

    def __str__(self) -> str:
        tail = f" — {self.note}" if self.note else ""
        return f"{self.date} {self.name} [{self.isin}]: {self.action} {self.detail}{tail}"


class BasketBook:
    def __init__(self, entries: Iterable[BasketEntry] = (), path: str = "", log_path: str = "", log: Iterable[BasketEvent] = ()):
        self.path = path
        self.log_path = log_path or (os.path.splitext(path)[0] + "_log.csv" if path else "")
        self.entries: list[BasketEntry] = list(entries)
        self.log: list[BasketEvent] = list(log)

    # ---- загрузка/сохранение -------------------------------------------------------------------------------
    @classmethod
    def from_csv(cls, path: str, log_path: str = "") -> "BasketBook":
        entries: list[BasketEntry] = []
        if path and os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        entries.append(BasketEntry(r["isin"].strip().upper(), (r.get("secid") or "").strip(), (r.get("name") or "").strip(),
                                                   (r.get("list") or "A").strip().upper(), float(r.get("weight") or 0),
                                                   (r.get("status") or "active").strip(), date.fromisoformat(r["added"]),
                                                   date.fromisoformat(r.get("changed") or r["added"]), (r.get("note") or "").strip()))
                    except (KeyError, ValueError):
                        continue
        log: list[BasketEvent] = []
        log_path = log_path or (os.path.splitext(path)[0] + "_log.csv" if path else "")
        if log_path and os.path.exists(log_path):
            with open(log_path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        log.append(BasketEvent(date.fromisoformat(r["date"]), r["isin"].strip().upper(), r.get("name") or "",
                                               r.get("action") or "", r.get("detail") or "", r.get("note") or ""))
                    except (KeyError, ValueError):
                        continue
        return cls(entries, path, log_path, log)

    def save(self) -> None:
        if not self.path:
            raise ValueError("книга корзины без пути (data.basket_csv)")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for e in sorted(self.entries, key=lambda e: (e.list, STATUSES.index(e.status) if e.status in STATUSES else 9, -e.weight, e.name)):
                w.writerow(e.as_dict())
        if self.log_path:
            with open(self.log_path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                w.writeheader()
                for ev in self.log:
                    w.writerow({"date": ev.date.isoformat(), "isin": ev.isin, "name": ev.name, "action": ev.action, "detail": ev.detail, "note": ev.note})

    # ---- выборки ---------------------------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.entries)

    def by_isin(self, isin: str) -> Optional[BasketEntry]:
        isin = isin.strip().upper()
        return next((e for e in self.entries if e.isin == isin), None)

    def find(self, query: str) -> list[BasketEntry]:
        """Записи по ISIN/SECID/части названия (несколько запросов через запятую)."""
        qs = [q.strip().upper() for q in query.replace("|", ",").split(",") if q.strip()]
        return [e for e in self.entries if any(q == e.isin or q == e.secid.upper() or q in e.name.upper() for q in qs)]

    def live(self, list_: Optional[str] = None) -> list[BasketEntry]:
        return [e for e in self.entries if e.live and (list_ is None or e.list == list_)]

    def active(self, list_: Optional[str] = None) -> list[BasketEntry]:
        return [e for e in self.entries if e.status == "active" and (list_ is None or e.list == list_)]

    def summary(self) -> str:
        parts = []
        for lst in LISTS:
            n = {s: sum(1 for e in self.entries if e.list == lst and e.status == s) for s in STATUSES}
            if any(n.values()):
                w = sum(e.weight for e in self.entries if e.list == lst and e.status == "active")
                parts.append(f"{lst}: в портфеле {n['active']} (вес {w:g}%), пауза {n['hold']}, ожидание {n['wait']}, исключено {n['removed']}")
        return "; ".join(parts) if parts else "корзина пуста"

    # ---- изменения (каждое — строка журнала) -----------------------------------------------------------------
    def add(self, isin: str, secid: str, name: str, list_: str = "A", weight: float = 0.0, status: str = "active",
            note: str = "", on: Optional[date] = None) -> BasketEvent:
        on = on or date.today()
        isin, list_ = isin.strip().upper(), list_.strip().upper()
        if list_ not in LISTS:
            raise ValueError(f"список {list_}: допустимы {', '.join(LISTS)}")
        if status not in STATUSES:
            raise ValueError(f"статус {status}: допустимы {', '.join(STATUSES)}")
        e = self.by_isin(isin)
        if e is not None:
            # уже в книге: «добавить» = вернуть в нужный статус/список с новым весом, история сохраняется
            ev = self.update(isin, status=status, list_=list_, weight=weight, note=note, on=on)
            return ev or BasketEvent(on, isin, e.name, "add", "без изменений", note)
        self.entries.append(BasketEntry(isin, secid, name, list_, float(weight), status, on, on, note))
        ev = BasketEvent(on, isin, name, "add", f"{list_} {weight:g}% {STATUS_RU.get(status, status)}", note)
        self.log.append(ev)
        return ev

    def update(self, isin: str, status: Optional[str] = None, list_: Optional[str] = None, weight: Optional[float] = None,
               note: str = "", on: Optional[date] = None) -> Optional[BasketEvent]:
        """Сменить статус/список/вес; note дописывается к заметке записи. Возвращает последнее событие (None — ничего не менялось)."""
        on = on or date.today()
        e = self.by_isin(isin)
        if e is None:
            raise KeyError(f"{isin}: нет в корзине")
        last: Optional[BasketEvent] = None
        if status is not None and status != e.status:
            if status not in STATUSES:
                raise ValueError(f"статус {status}: допустимы {', '.join(STATUSES)}")
            last = BasketEvent(on, e.isin, e.name, "status", f"{STATUS_RU.get(e.status, e.status)} → {STATUS_RU.get(status, status)}", note)
            e.status = status
            self.log.append(last)
        if list_ is not None and list_.strip().upper() != e.list:
            list_ = list_.strip().upper()
            if list_ not in LISTS:
                raise ValueError(f"список {list_}: допустимы {', '.join(LISTS)}")
            last = BasketEvent(on, e.isin, e.name, "list", f"{e.list} → {list_}", note)
            e.list = list_
            self.log.append(last)
        if weight is not None and float(weight) != e.weight:
            last = BasketEvent(on, e.isin, e.name, "weight", f"{e.weight:g}% → {float(weight):g}%", note)
            e.weight = float(weight)
            self.log.append(last)
        if note and last is None:
            last = BasketEvent(on, e.isin, e.name, "note", "", note)
            self.log.append(last)
        if last is not None:
            e.changed = on
            if note:
                e.note = f"{e.note}; {on:%d.%m}: {note}" if e.note else f"{on:%d.%m}: {note}"
        return last


# ---- живой срез корзины: рынок + флаги -----------------------------------------------------------------------------
@dataclass
class BasketRow:
    entry: BasketEntry
    row: object = None                # ScreenRow (из скрина или посчитанный по бумаге вне сита); None — нет цены/бумаги
    in_screen: bool = False
    sieve: str = ""                   # причина отсева, если бумага вне сита
    peers_excess: Optional[float] = None
    peers_n: int = 0
    chg30: Optional[float] = None
    z: Optional[float] = None
    flags: list[str] = field(default_factory=list)

    @property
    def secid(self) -> str:
        return self.row.secid if self.row is not None else self.entry.secid

    @property
    def name(self) -> str:
        return self.row.bond.name if self.row is not None else self.entry.name


def _flags(br: BasketRow, min_turnover: float, news_alert: float) -> list[str]:
    F: list[str] = []
    r = br.row
    if r is None:
        return ["нет на MOEX / нет цены"]
    if br.sieve:
        low = br.sieve.lower()
        if "дефолт" in low:
            F.append(f"⛔ {br.sieve}")
        elif "оборот" in low:
            F.append(f"оборот {r.quote.turnover / 1e6:.1f} млн < {min_turnover / 1e6:g}")
        else:
            F.append(f"вне сита: {br.sieve}")
    if r.rating is None and not r.bond.is_ofz:
        F.append("нет рейтинга в книге")
    if br.chg30 is not None and br.chg30 >= 100:
        F.append(f"спред +{br.chg30:.0f} б.п. за 30 дн." + (f" (z {br.z:+.1f})" if br.z is not None else ""))
    if r.news is not None and r.news.n and r.news.score <= news_alert:
        F.append(f"новости {r.news.score:+.1f}")
    if getattr(r, "stop_events", None):
        F.append("существенный факт e-disclosure")
    return F


def basket_rows(book: BasketBook, snap, rows: list, hist: Optional[dict] = None, min_turnover: float = 1e6,
                news_alert: float = -3.0, peers_kw: Optional[dict] = None) -> list[BasketRow]:
    """Живой срез по всем неисключённым записям корзины.

    rows — строки скрина; для бумаг вне сита метрики считаются по котировке (как для держащихся позиций);
    hist — SpreadStats по secid (история спреда), считается вызывающей стороной только по бумагам корзины."""
    from .analytics.bond_math import compute_metrics
    from .analytics.peers import peer_stats
    from .screener import ScreenRow
    by_id = {r.secid: r for r in rows}
    by_isin = {(b.isin or "").upper(): (b, q) for b, q in snap.universe}
    by_secid = {b.secid.upper(): (b, q) for b, q in snap.universe}
    corp = [r for r in rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None]
    out: list[BasketRow] = []
    for e in book.live():
        hit = by_isin.get(e.isin) or by_secid.get(e.secid.upper())
        br = BasketRow(e)
        if hit is None:
            br.flags = _flags(br, min_turnover, news_alert)
            out.append(br)
            continue
        b, q = hit
        r = by_id.get(b.secid)
        if r is not None:
            br.row, br.in_screen = r, True
            if r.metrics.g_spread is not None and not r.bond.is_ofz:
                ps = peer_stats(r, corp, **(peers_kw or {}))
                if ps.n:
                    br.peers_excess, br.peers_n = ps.excess, ps.n
        else:
            bb = snap.enrich(b) if snap.enrich is not None and not b.has_full_schedule else b
            m = compute_metrics(bb, q, snap.settle, snap.curve)
            if m is not None:
                rating = snap.ratings.lookup(bb) if snap.ratings is not None and not bb.is_ofz else None
                issuer = snap.issuers.lookup(bb) if snap.issuers is not None and not bb.is_ofz else None
                inn = issuer.inn if issuer else ""
                r = ScreenRow(bb, q, m, rating=rating, inn=inn)
                if snap.news is not None and not bb.is_ofz:
                    r.news = snap.news.issuer_score(snap.settle, name=bb.full_name or bb.name, inn=inn)
                br.row = r
            br.sieve = (snap.rejected or {}).get(b.secid, "не в срезе скрина")
        hs = (hist or {}).get(b.secid)
        if hs is not None:
            br.chg30, br.z = hs.chg30, hs.z
        br.flags = _flags(br, min_turnover, news_alert)
        out.append(br)
    return out

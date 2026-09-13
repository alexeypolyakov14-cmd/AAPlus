"""Книга кредитных метрик из пресс-релизов агентств и «подразумеваемая» ступень рейтинга.

Зачем: ранжирование по премии к пирам опирается на выданный рейтинг. Рейтинг бывает куплен сменой агентства или
запаздывает. Вторая ось — что говорят цифры самого релиза: долг/EBITDA, покрытие процентов, рентабельность.
По ним считается подразумеваемая ступень (грубые бенчмарки для нефинансовых компаний) и разрыв с выданной:
  gap = grade(выданный) − grade(подразумеваемый)  (в ступенях SCALE; < 0 — метрики хуже рейтинга, > 0 — лучше).
Числа достаются регулярными выражениями из предложений релиза (Эксперт РА пишет их стабильно, НКР/АКРА — не всегда).
Что не распозналось — остаётся пустым, эмитент уходит в ручную проверку, а не получает выдуманную ступень.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Iterable, Optional

from .ratings import GRADE, SCALE, normalize_rating

log = logging.getLogger(__name__)

FINANCIAL_SECTORS = {"bank", "leasing", "mfo", "finance", "gov", "subfed"}   # бенчмарки ниже — только для нефинансовых

_NUM = r"(\d+(?:[.,]\d+)?)"
_X = r"\s*[xхX]\b"
_RE_NET_LEV = re.compile(r"чист[а-я]*\s+долг[а-я]*\s*(?:/|к|в\s+терминах[^.]*?/)\s*EBITDA[^.;]*?" + _NUM + _X, re.I)
_RE_NET_LEV2 = re.compile(r"(?:чистый\s+долг\s*/\s*EBITDA|ND\s*/\s*EBITDA)[^.;]*?" + _NUM + _X, re.I)
_RE_GROSS_LEV = re.compile(r"(?:совокупн[а-я]*|общ[а-я]*)\s+долг[а-я]*\s*(?:/|к)\s*(?:EBITDA|OIBDA)[^.;]*?" + _NUM + _X, re.I)
_RE_GROSS_LEV2 = re.compile(r"(?:долг|долга)\s*(?:/|к)\s*(?:EBITDA|OIBDA)[^.;]*?" + _NUM + _X, re.I)
_RE_PREV = re.compile(_NUM + _X + r"\s*годом\s+ранее", re.I)
_RE_COV = re.compile(r"(?:покрыти[а-я]*\s+процент[а-я]*[^.;]*?|EBITDA\s*/\s*(?:%%|проценты|процентн[а-я]*\s+расход[а-я]*)[^.;]*?)" + _NUM + _X, re.I)
_RE_COV_DOWN = re.compile(r"(?:покрыти[а-я]*\s+процент[а-я]*|EBITDA\s*/\s*%%)[^.;]*?(?:снизил[а-я]*|составил[а-я]*|вырос[а-я]*)\s+до\s+" + _NUM + _X, re.I)
_RE_MARGIN = re.compile(r"рентабельност[а-я]*\s+по\s+(?:EBITDA|OIBDA)[^.;%]*?" + _NUM + r"\s*%", re.I)
_RE_REVENUE = re.compile(r"выручк[а-я]*[^.;]*?(?:до|составил[а-я]*|уровн[а-я]*)\s+" + _NUM + r"\s*млрд", re.I)
_RE_EBITDA_ABS = re.compile(r"(?:показатель\s+)?EBITDA[^.;]*?составил[а-я]*\s+" + _NUM + r"\s*млрд", re.I)
_RE_PERIOD_YEAR = re.compile(r"по\s+итогам\s+(\d{4})\s+года", re.I)
_RE_PERIOD_DATE = re.compile(r"на\s+(\d{2}\.\d{2}\.\d{4})", re.I)
_RE_TITLE_RATING = re.compile(r"\bru([ABC]{1,3}[+-]?)(?![A-Za-z])|\b([ABC]{1,3}[+-]?)\s*(?:\.ru\b|\(RU\))", re.I)
_RE_PERIOD_YEAR2 = re.compile(r"за\s+(\d{4})\s+год", re.I)
# НКР пишет метрики без «x»: «отношение совокупного долга к OIBDA … составило 2,5», «покрытие процентов … составило 1,9»
_RE_GROSS_LEV_NKR = re.compile(r"долг[а-я]*\s+к\s+(?:EBITDA|OIBDA)\)?[^.;]*?составил[а-я]*\s+" + _NUM + r"(?![\d.,]*\s*(?:%|млрд|млн))", re.I)
_RE_COV_NKR = re.compile(r"покрыти[ея]\s+процентов[^.;]*?(?:OIBDA|EBITDA)\)?[^.;]*?составил[а-я]*\s+" + _NUM + r"(?![\d.,]*\s*(?:%|млрд|млн))", re.I)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def _first(rx: re.Pattern, sentences: Iterable[str]) -> Optional[float]:
    for s in sentences:
        m = rx.search(s)
        if m:
            return _f(m.group(1))
    return None


@dataclass
class AgencyMetrics:
    key: str                         # ключ эмитента (Bond.issuer_key)
    subject: str                     # как назван в релизе
    agency: str
    url: str
    release_date: Optional[date] = None
    period: str = ""                 # отчётный период из релиза («2025», «30.06.2025»)
    rating: str = ""                 # рейтинг из заголовка релиза (нормализованный)
    net_debt_ebitda: Optional[float] = None
    net_debt_ebitda_prev: Optional[float] = None
    debt_ebitda: Optional[float] = None
    coverage: Optional[float] = None
    ebitda_margin: Optional[float] = None
    revenue_bln: Optional[float] = None
    ebitda_bln: Optional[float] = None
    sector: str = ""

    @property
    def leverage(self) -> Optional[float]:
        """Долговая нагрузка для бенчмарков: чистый долг/EBITDA, иначе совокупный долг/EBITDA."""
        return self.net_debt_ebitda if self.net_debt_ebitda is not None else self.debt_ebitda

    @property
    def has_metrics(self) -> bool:
        return self.leverage is not None or self.coverage is not None

    def describe(self) -> str:
        parts = []
        if self.net_debt_ebitda is not None:
            s = f"чистый долг/EBITDA {self.net_debt_ebitda:.1f}x"
            if self.net_debt_ebitda_prev is not None:
                s += f" ({self.net_debt_ebitda_prev:.1f}x годом ранее)"
            parts.append(s)
        elif self.debt_ebitda is not None:
            parts.append(f"долг/EBITDA {self.debt_ebitda:.1f}x")
        if self.coverage is not None:
            parts.append(f"покрытие {self.coverage:.1f}x")
        if self.ebitda_margin is not None:
            parts.append(f"маржа EBITDA {self.ebitda_margin:.0f}%")
        if self.revenue_bln is not None:
            parts.append(f"выручка {self.revenue_bln:g} млрд")
        src = f"{self.agency} {self.release_date or ''}".strip()
        return (", ".join(parts) if parts else "чисел в релизе нет") + (f" [{src}, за {self.period}]" if self.period else f" [{src}]")


def extract_metrics(sentences: list[str], title: str = "") -> dict:
    """Числа из предложений релиза. Возвращает словарь полей AgencyMetrics (без key/subject/agency/url)."""
    out: dict = {}
    net = _first(_RE_NET_LEV, sentences)
    if net is None:
        net = _first(_RE_NET_LEV2, sentences)
    if net is not None:
        out["net_debt_ebitda"] = net
        for s in sentences:
            if _RE_NET_LEV.search(s) or _RE_NET_LEV2.search(s):
                m = _RE_PREV.search(s)
                if m:
                    out["net_debt_ebitda_prev"] = _f(m.group(1))
                break
    gross = _first(_RE_GROSS_LEV, sentences)
    if gross is None and net is None:
        gross = _first(_RE_GROSS_LEV2, sentences)
    if gross is None and net is None:
        gross = _first(_RE_GROSS_LEV_NKR, sentences)
    if gross is not None:
        out["debt_ebitda"] = gross
    cov = _first(_RE_COV_DOWN, sentences)
    if cov is None:
        cov = _first(_RE_COV, sentences)
    if cov is None:
        cov = _first(_RE_COV_NKR, sentences)
    if cov is not None:
        out["coverage"] = cov
    m = _first(_RE_MARGIN, sentences)
    if m is not None:
        out["ebitda_margin"] = m
    rev = _first(_RE_REVENUE, sentences)
    if rev is not None:
        out["revenue_bln"] = rev
    eb = _first(_RE_EBITDA_ABS, sentences)
    if eb is not None:
        out["ebitda_bln"] = eb
    for s in sentences:
        y = _RE_PERIOD_YEAR.search(s)
        if y:
            out["period"] = y.group(1)
            break
        d = _RE_PERIOD_DATE.search(s)
        if d:
            out["period"] = d.group(1)
            break
    if "period" not in out:
        for s in sentences:
            y = _RE_PERIOD_YEAR2.search(s)
            if y:
                out["period"] = y.group(1)
                break
    if title:
        t = _RE_TITLE_RATING.search(title)
        if t:
            r = normalize_rating(t.group(1) or t.group(2))
            if r:
                out["rating"] = r
    return out


# --- подразумеваемая ступень -------------------------------------------------------------------------------------
# Грубые бенчмарки для нефинансовых компаний (близко к таблицам Эксперт РА/АКРА для «среднего» бизнес-профиля).
LEVERAGE_STEPS = [(1.0, "AA"), (1.5, "AA-"), (2.0, "A+"), (2.5, "A"), (3.0, "A-"), (3.5, "BBB+"), (4.0, "BBB"), (4.5, "BBB-"),
                  (5.0, "BB+"), (6.0, "BB"), (float("inf"), "B+")]
COVERAGE_STEPS = [(7.0, "AA"), (5.0, "A+"), (4.0, "A"), (3.0, "A-"), (2.5, "BBB+"), (2.0, "BBB"), (1.5, "BBB-"), (1.2, "BB+"),
                  (1.0, "BB-"), (0.0, "B")]


def implied_grade(m: AgencyMetrics) -> Optional[str]:
    """Ступень по худшей из двух метрик (нагрузка, покрытие); None — нет чисел или финансовый сектор."""
    if m.sector in FINANCIAL_SECTORS:
        return None
    cands: list[str] = []
    lev = m.leverage
    if lev is not None:
        cands.append(next(r for lim, r in LEVERAGE_STEPS if lev <= lim))
    if m.coverage is not None:
        cands.append(next(r for lim, r in COVERAGE_STEPS if m.coverage >= lim))
    if not cands:
        return None
    return max(cands, key=lambda r: GRADE[r])   # худшая = больший индекс в SCALE


def rating_gap(actual: Optional[str], implied: Optional[str]) -> Optional[int]:
    """grade(выданный) − grade(подразумеваемый): < 0 — метрики хуже рейтинга, > 0 — лучше."""
    a, i = normalize_rating(actual), normalize_rating(implied)
    if a is None or i is None:
        return None
    return GRADE[a] - GRADE[i]


# --- книга -------------------------------------------------------------------------------------------------------
class MetricsBook:
    def __init__(self, items: Iterable[AgencyMetrics] = ()):
        self.items: list[AgencyMetrics] = list(items)

    def __len__(self) -> int:
        return len(self.items)

    def upsert(self, m: AgencyMetrics) -> None:
        """Одна запись на (ключ, агентство): свежий релиз вытесняет старый."""
        for i, x in enumerate(self.items):
            if x.key == m.key and x.agency == m.agency:
                if m.release_date and x.release_date and m.release_date < x.release_date:
                    return
                self.items[i] = m
                return
        self.items.append(m)

    def lookup(self, key: str) -> Optional[AgencyMetrics]:
        """Свежая запись с числами по ключу эмитента; если чисел нет нигде — любая свежая."""
        mine = [x for x in self.items if x.key == key]
        if not mine:
            return None
        with_nums = [x for x in mine if x.has_metrics]
        pool = with_nums or mine
        return max(pool, key=lambda x: x.release_date or date.min)

    @classmethod
    def from_csv(cls, path: str) -> "MetricsBook":
        book = cls()
        if not path or not os.path.exists(path):
            return book
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                kw: dict = {}
                for fld in fields(AgencyMetrics):
                    v = row.get(fld.name, "")
                    if v in ("", None):
                        continue
                    if fld.name == "release_date":
                        try:
                            kw[fld.name] = date.fromisoformat(v)
                        except ValueError:
                            continue
                    elif fld.type in ("Optional[float]",):
                        try:
                            kw[fld.name] = float(v)
                        except ValueError:
                            continue
                    else:
                        kw[fld.name] = v
                if kw.get("key") and kw.get("agency"):
                    book.items.append(AgencyMetrics(**{**{"subject": "", "url": ""}, **kw}))
        return book

    def to_csv(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[x.name for x in fields(AgencyMetrics)])
            w.writeheader()
            for m in sorted(self.items, key=lambda x: (x.key, x.agency)):
                d = asdict(m)
                d["release_date"] = m.release_date.isoformat() if m.release_date else ""
                w.writerow({k: ("" if v is None else v) for k, v in d.items()})


def metrics_from_digests(key: str, agency: str, sector: str, digests) -> Optional[AgencyMetrics]:
    """Первый (свежий) релиз, в котором нашлись числа; если ни в одном — запись по самому свежему без чисел."""
    best = None
    for d in digests:
        ext = extract_metrics(d.sentences, d.title)
        m = AgencyMetrics(key=key, subject=d.title, agency=agency, url=d.url, release_date=d.date, sector=sector, **ext)
        if m.has_metrics:
            return m
        if best is None:
            best = m
    return best

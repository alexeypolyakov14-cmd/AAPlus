"""Базовые модели данных: облигация, котировка, денежный поток."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class CashFlow:
    """Один платёж по облигации на дату (купон и/или погашение части номинала)."""
    date: date
    coupon: float = 0.0
    principal: float = 0.0

    @property
    def total(self) -> float:
        return self.coupon + self.principal


FLOATER_NAME_RE = re.compile(r"(ПК|флоат|float|FRN|-ФЛ|КС\+|RUONIA)", re.IGNORECASE)
# Структурные бумаги: секьюритизация (СФО), ипотечные агенты (ИА), транши классов А/Б — амортизация зависит от
# досрочных погашений пула, наша модель денежных потоков к ним неприменима.
STRUCTURED_RE = re.compile(r"(^|\s)(СФО|ИА|СБСекр|СБ Секьюр|Сплит|ТБ-\d|ДОМ\.РФ ИА)|секьюрит|ипотечн\w+ агент|специализированн\w+ финансов\w+ обществ|"
                           r"\bкл\.? ?[АБA-C]\d?\b|класс[аы]? [АБ]\b|ИОС\d|\bИОС\b|инвестиционн\w+ облигац|структурн\w+ облигац", re.IGNORECASE)


@dataclass
class Bond:
    """Статические параметры облигации (из MOEX ISS securities + bondization)."""
    secid: str
    name: str = ""
    isin: str = ""
    board: str = ""
    full_name: str = ""                  # SECNAME MOEX: обычно содержит название эмитента
    face_value: float = 1000.0           # текущий (с учётом амортизации) номинал
    initial_face_value: float = 1000.0
    currency: str = "SUR"
    coupon_percent: Optional[float] = None  # ставка текущего купона, % годовых
    coupon_value: Optional[float] = None    # сумма текущего купона, руб.
    coupon_period: Optional[int] = None     # дней между купонами
    next_coupon: Optional[date] = None
    maturity: Optional[date] = None
    offer_date: Optional[date] = None       # ближайшая оферта (put)
    buyback_price: Optional[float] = None   # цена выкупа по оферте, % от номинала
    issue_size: Optional[float] = None
    list_level: Optional[int] = None
    sectype: str = ""
    lot_size: int = 1
    # Полный график из bondization (если загружен)
    coupons: list[tuple[date, Optional[float]]] = field(default_factory=list)      # (дата, сумма или None — плавающий)
    amortizations: list[tuple[date, float]] = field(default_factory=list)          # (дата, сумма погашения номинала)
    has_full_schedule: bool = False

    # ---- классификация ----
    @property
    def is_ofz(self) -> bool:
        return self.secid.startswith("SU") or self.name.upper().startswith("ОФЗ")

    @property
    def is_floater(self) -> bool:
        """Плавающий купон: ОФЗ-ПК (SU29xxx), признаки в названии или неизвестные будущие купоны."""
        if self.secid.startswith("SU29"):
            return True
        if FLOATER_NAME_RE.search(self.name or ""):
            return True
        if self.has_full_schedule:
            future = [c for c in self.coupons if c[1] is None]
            # первый купон, как правило, известен; если дальше неизвестны — флоатер
            return len(future) > 0 and len(future) >= max(1, len(self.coupons) - 2)
        return False

    @property
    def is_structured(self) -> bool:
        """Секьюритизация / ипотечные агенты / транши: по маркерам в кратком и полном названии."""
        if self.is_ofz:
            return False
        return bool(STRUCTURED_RE.search(self.name or "") or STRUCTURED_RE.search(self.full_name or ""))

    @property
    def is_linker(self) -> bool:
        """ОФЗ-ИН (индексируемый номинал)."""
        return self.secid.startswith("SU52") or "ИН" in (self.name or "").upper().split()

    @property
    def has_amortization(self) -> bool:
        if self.has_full_schedule:
            return len(self.amortizations) > 1
        return self.face_value < self.initial_face_value - 1e-9

    @property
    def has_offer(self) -> bool:
        return self.offer_date is not None

    @property
    def issuer_key(self) -> str:
        """Грубый ключ эмитента для лимитов концентрации (ОФЗ — Минфин).

        Краткие имена MOEX склеивают эмитента и серию без пробела («БалтЛизП16», «АРЛФ1Р02», «iКарРус1P6»),
        поэтому берём буквенный префикс имени до первой цифры/знака и отбрасываем хвост-маркер серии (БО, ПБО, БП, П, Б, Р).
        """
        if self.is_ofz:
            return "МИНФИН"
        return issuer_key_of(self.name or self.secid)


_ISSUER_PREFIX_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё ]*")
_SERIES_TAIL_RE = re.compile(r"\s*(ПБО|БО|БП|ПБ|П|Б|Р)$")


def issuer_key_of(name: str) -> str:
    m = _ISSUER_PREFIX_RE.match(name or "")
    key = (m.group(0) if m else (name or "")).strip()
    if not key:
        return (name or "").split()[0].upper() if name else ""
    stripped = _SERIES_TAIL_RE.sub("", key).strip()
    if len(stripped) >= 3:
        key = stripped
    return key.upper().rstrip(",.-")


@dataclass
class Quote:
    """Рыночные данные по облигации на дату."""
    secid: str
    trade_date: date
    price: Optional[float] = None          # чистая цена, % от номинала
    bid: Optional[float] = None
    ask: Optional[float] = None
    accrued: float = 0.0                   # НКД, руб. на бумагу
    ytm_moex: Optional[float] = None       # эффективная доходность по данным биржи, % годовых
    duration_moex: Optional[float] = None  # дюрация по данным биржи, лет
    turnover: float = 0.0                  # оборот за день, руб.
    num_trades: int = 0

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.price

    @property
    def bid_ask_spread_pct(self) -> Optional[float]:
        if self.bid and self.ask and self.bid > 0:
            return (self.ask - self.bid) / self.bid * 100
        return None


@dataclass
class BondMetrics:
    """Расчётные метрики облигации."""
    secid: str
    clean_price: float               # % от номинала
    dirty_price: float               # руб. на бумагу
    ytm: float                       # эффективная доходность к погашению, % годовых
    ytm_to_offer: Optional[float]    # к оферте (если есть)
    yield_worst: float               # доходность к «худшей» дате (оферта, если она первична)
    macaulay_duration: float         # лет, к той же дате, что и yield_worst
    modified_duration: float
    convexity: float
    dv01: float                      # руб. на бумагу при сдвиге на 1 б.п.
    years_to_maturity: float
    current_yield: float             # купон / чистая цена
    g_spread: Optional[float] = None # б.п. к кривой ОФЗ

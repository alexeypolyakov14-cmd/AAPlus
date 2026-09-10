"""Ключевая ставка Банка России (SOAP-сервис DailyInfo) и разбор её динамики."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

from .cache import NullCache

log = logging.getLogger(__name__)

CBR_SOAP_URL = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
_KR_RE = re.compile(r"<KR\b[^>]*>\s*<DT>([\d\-]+)[^<]*</DT>\s*<Rate>([\d.]+)</Rate>", re.S)


def parse_keyrate_xml(xml_text: str) -> list[tuple[date, float]]:
    """Возвращает список (дата, ставка) по возрастанию даты."""
    rows = [(date.fromisoformat(m.group(1)[:10]), float(m.group(2))) for m in _KR_RE.finditer(xml_text)]
    rows.sort()
    return rows


def fetch_keyrate_history(start: date, end: Optional[date] = None, cache=None, timeout: float = 20) -> list[tuple[date, float]]:
    end = end or date.today()
    cache = cache or NullCache()
    key = f"cbr_keyrate:{start}:{end}"
    cached = cache.get(key, 6 * 3600)
    if cached:
        return [(date.fromisoformat(d), r) for d, r in cached]
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:web="http://web.cbr.ru/">
  <soap:Body><web:KeyRateXML><web:fromDate>{start.isoformat()}</web:fromDate><web:ToDate>{end.isoformat()}</web:ToDate></web:KeyRateXML></soap:Body>
</soap:Envelope>"""
    resp = requests.post(CBR_SOAP_URL, data=body.encode("utf-8"), timeout=timeout,
                         headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "http://web.cbr.ru/KeyRateXML"})
    resp.raise_for_status()
    rows = parse_keyrate_xml(resp.text)
    cache.set(key, [(d.isoformat(), r) for d, r in rows])
    return rows


@dataclass
class KeyRateView:
    """Текущая ставка и фаза цикла ДКП, выведенная из истории решений."""
    current: float
    last_change_date: Optional[date]
    last_change: float               # п.п., >0 — повышение
    consecutive_moves: int           # число подряд идущих решений в одну сторону (0 — последнее решение «сохранить»)
    regime: str                      # "easing" | "tightening" | "hold"
    peak: float                      # максимум за историю окна
    trough: float


def analyze_keyrate(history: list[tuple[date, float]], hold_after_days: int = 120) -> KeyRateView:
    """История дневных значений ставки -> сводка по решениям.

    Регламент: ставка меняется в даты заседаний; в дневном ряду это ступеньки.
    regime = направление последнего изменения, если оно было не позже
    hold_after_days назад, иначе "hold".
    """
    if not history:
        raise ValueError("пустая история ключевой ставки")
    changes: list[tuple[date, float]] = []  # (дата, дельта)
    prev = history[0][1]
    for d, r in history[1:]:
        if abs(r - prev) > 1e-9:
            changes.append((d, r - prev))
            prev = r
    current = history[-1][1]
    if not changes:
        return KeyRateView(current, None, 0.0, 0, "hold", max(r for _, r in history), min(r for _, r in history))
    last_d, last_delta = changes[-1]
    n = 0
    for _, delta in reversed(changes):
        if (delta > 0) == (last_delta > 0):
            n += 1
        else:
            break
    age = (history[-1][0] - last_d).days
    if age > hold_after_days:
        regime = "hold"
    else:
        regime = "tightening" if last_delta > 0 else "easing"
    return KeyRateView(current, last_d, last_delta, n, regime,
                       max(r for _, r in history), min(r for _, r in history))

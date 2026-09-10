"""Клиент MOEX ISS для облигаций.

Документация ISS: https://iss.moex.com/iss/reference/
Все сетевые вызовы идут через fetch_json(), парсеры — чистые функции,
которые можно тестировать на сохранённых ответах.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Iterable, Optional

import requests

from ..models import Bond, Quote
from .cache import NullCache

log = logging.getLogger(__name__)

ISS_BASE = "https://iss.moex.com"
DEFAULT_BOARDS = ("TQOB", "TQCB")  # Т+ гособлигации, Т+ корпоративные облигации

SEC_COLUMNS = ("SECID,SHORTNAME,SECNAME,ISIN,BOARDID,PREVPRICE,PREVWAPRICE,PREVLEGALCLOSEPRICE,MATDATE,"
               "COUPONPERCENT,COUPONVALUE,COUPONPERIOD,NEXTCOUPON,ACCRUEDINT,FACEVALUE,INITIALFACEVALUE,FACEUNIT,"
               "CURRENCYID,LOTSIZE,LOTVALUE,ISSUESIZE,ISSUESIZEPLACED,OFFERDATE,BUYBACKPRICE,BUYBACKDATE,"
               "SECTYPE,LISTLEVEL,STATUS")
MD_COLUMNS = ("SECID,BID,OFFER,LAST,LCURRENTPRICE,MARKETPRICE,YIELD,YIELDATPREVWAPRICE,DURATION,VALTODAY,"
              "VOLTODAY,NUMTRADES,TRADINGSTATUS,SYSTIME")


# ---------------------------------------------------------------------------
# Утилиты парсинга
# ---------------------------------------------------------------------------

def table(payload: dict, block: str) -> list[dict]:
    """Блок ISS {columns, data} -> список словарей."""
    b = payload.get(block) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row)) for row in (b.get("data") or [])]


def _date(v: Any) -> Optional[date]:
    if not v or v in ("0000-00-00",):
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_bond_row(row: dict) -> Bond:
    face = _num(row.get("FACEVALUE")) or 1000.0
    initial = _num(row.get("INITIALFACEVALUE")) or face
    return Bond(
        secid=row.get("SECID", ""),
        name=row.get("SHORTNAME") or row.get("SECNAME") or "",
        isin=row.get("ISIN") or "",
        board=row.get("BOARDID") or "",
        face_value=face,
        initial_face_value=max(initial, face),
        currency=row.get("CURRENCYID") or row.get("FACEUNIT") or "SUR",
        coupon_percent=_num(row.get("COUPONPERCENT")),
        coupon_value=_num(row.get("COUPONVALUE")),
        coupon_period=int(_num(row.get("COUPONPERIOD")) or 0) or None,
        next_coupon=_date(row.get("NEXTCOUPON")),
        maturity=_date(row.get("MATDATE")),
        offer_date=_date(row.get("OFFERDATE")) or _date(row.get("BUYBACKDATE")),
        buyback_price=_num(row.get("BUYBACKPRICE")),
        issue_size=_num(row.get("ISSUESIZEPLACED")) or _num(row.get("ISSUESIZE")),
        list_level=int(_num(row.get("LISTLEVEL")) or 0) or None,
        sectype=row.get("SECTYPE") or "",
        lot_size=int(_num(row.get("LOTSIZE")) or 1),
    )


def parse_quote_row(sec: dict, md: Optional[dict], trade_date: date) -> Quote:
    md = md or {}
    price = _num(md.get("LAST")) or _num(md.get("LCURRENTPRICE")) or _num(md.get("MARKETPRICE")) \
        or _num(sec.get("PREVLEGALCLOSEPRICE")) or _num(sec.get("PREVWAPRICE")) or _num(sec.get("PREVPRICE"))
    dur_days = _num(md.get("DURATION"))
    return Quote(
        secid=sec.get("SECID", ""),
        trade_date=trade_date,
        price=price,
        bid=_num(md.get("BID")),
        ask=_num(md.get("OFFER")),
        accrued=_num(sec.get("ACCRUEDINT")) or 0.0,
        ytm_moex=_num(md.get("YIELD")) or _num(md.get("YIELDATPREVWAPRICE")),
        duration_moex=(dur_days / 365.0) if dur_days else None,
        turnover=_num(md.get("VALTODAY")) or 0.0,
        num_trades=int(_num(md.get("NUMTRADES")) or 0),
    )


def parse_board_securities(payload: dict, trade_date: Optional[date] = None) -> list[tuple[Bond, Quote]]:
    """Ответ /iss/engines/stock/markets/bonds/boards/{board}/securities.json -> [(Bond, Quote)]."""
    secs = table(payload, "securities")
    mds = {r.get("SECID"): r for r in table(payload, "marketdata")}
    trade_date = trade_date or date.today()
    out = []
    for s in secs:
        if not s.get("SECID"):
            continue
        bond = parse_bond_row(s)
        quote = parse_quote_row(s, mds.get(s["SECID"]), trade_date)
        out.append((bond, quote))
    return out


def parse_bondization(payload: dict) -> dict:
    """Ответ /iss/securities/{secid}/bondization.json -> {coupons, amortizations, offers}."""
    coupons = []
    for r in table(payload, "coupons"):
        d = _date(r.get("coupondate"))
        if d:
            coupons.append((d, _num(r.get("value"))))
    amort = []
    for r in table(payload, "amortizations"):
        d = _date(r.get("amortdate"))
        if d:
            amort.append((d, _num(r.get("value")) or 0.0))
    offers = []
    for r in table(payload, "offers"):
        d = _date(r.get("offerdate"))
        if d:
            offers.append({"date": d, "price": _num(r.get("price")), "type": r.get("offertype") or ""})
    return {"coupons": sorted(coupons), "amortizations": sorted(amort), "offers": sorted(offers, key=lambda o: o["date"])}


def apply_bondization(bond: Bond, sched: dict, today: Optional[date] = None) -> Bond:
    today = today or date.today()
    bond.coupons = sched.get("coupons", [])
    bond.amortizations = sched.get("amortizations", [])
    bond.has_full_schedule = bool(bond.coupons)
    future_offers = [o for o in sched.get("offers", []) if o["date"] > today]
    if future_offers and bond.offer_date is None:
        bond.offer_date = future_offers[0]["date"]
        bond.buyback_price = future_offers[0]["price"] or bond.buyback_price
    return bond


def parse_history(payload: dict) -> list[dict]:
    """Ответ /iss/history/.../securities/{secid}.json -> список дневных записей."""
    out = []
    for r in table(payload, "history"):
        d = _date(r.get("TRADEDATE"))
        if not d:
            continue
        dur_days = _num(r.get("DURATION"))
        out.append({
            "date": d,
            "close": _num(r.get("LEGALCLOSEPRICE")) or _num(r.get("CLOSE")) or _num(r.get("WAPRICE")),
            "ytm": _num(r.get("YIELDCLOSE")) or _num(r.get("YIELDATWAP")),
            "duration": (dur_days / 365.0) if dur_days else None,
            "accrued": _num(r.get("ACCINT")) or 0.0,
            "face": _num(r.get("FACEVALUE")),
            "volume": _num(r.get("VOLUME")) or 0.0,
            "value": _num(r.get("VALUE")) or 0.0,
        })
    return out


def parse_index_history(payload: dict) -> list[tuple[date, float]]:
    out = []
    for r in table(payload, "history"):
        d = _date(r.get("TRADEDATE"))
        c = _num(r.get("CLOSE"))
        if d and c is not None:
            out.append((d, c))
    return out


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class MoexClient:
    def __init__(self, cache=None, base: str = ISS_BASE, timeout: float = 20.0,
                 session: Optional[requests.Session] = None, min_interval: float = 0.15):
        self.cache = cache or NullCache()
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "bondtrader/0.1 (+python-requests)", "Accept": "application/json"})
        self.min_interval = min_interval
        self._last_call = 0.0

    # --- низкоуровневый запрос ---
    def fetch_json(self, path: str, params: Optional[dict] = None, ttl: float = 300, retries: int = 3) -> dict:
        params = dict(params or {})
        params.setdefault("iss.meta", "off")
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self.cache.get(key, ttl)
        if cached is not None:
            return cached
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.get(self.base + path, params=params, timeout=self.timeout)
                self._last_call = time.time()
                resp.raise_for_status()
                data = resp.json()
                self.cache.set(key, data)
                return data
            except (requests.RequestException, ValueError) as e:
                last_err = e
                log.warning("MOEX %s: попытка %d/%d: %s", path, attempt + 1, retries, e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"MOEX ISS недоступен: {path}: {last_err}")

    # --- справочники и котировки ---
    def board_bonds(self, board: str, trade_date: Optional[date] = None) -> list[tuple[Bond, Quote]]:
        payload = self.fetch_json(
            f"/iss/engines/stock/markets/bonds/boards/{board}/securities.json",
            {"iss.only": "securities,marketdata", "securities.columns": SEC_COLUMNS, "marketdata.columns": MD_COLUMNS},
            ttl=120,
        )
        return parse_board_securities(payload, trade_date)

    def bonds(self, boards: Iterable[str] = DEFAULT_BOARDS, trade_date: Optional[date] = None) -> list[tuple[Bond, Quote]]:
        out: list[tuple[Bond, Quote]] = []
        seen: set[str] = set()
        for b in boards:
            for bond, q in self.board_bonds(b, trade_date):
                if bond.secid in seen:
                    continue
                seen.add(bond.secid)
                out.append((bond, q))
        return out

    def bondization(self, secid: str) -> dict:
        payload = self.fetch_json(f"/iss/securities/{secid}/bondization.json", {"limit": "unlimited"}, ttl=86400)
        return parse_bondization(payload)

    def enrich(self, bond: Bond) -> Bond:
        """Подгружает полный график купонов/амортизаций/оферт в объект Bond."""
        try:
            return apply_bondization(bond, self.bondization(bond.secid))
        except Exception as e:  # noqa: BLE001 — не валим весь скрин из-за одной бумаги
            log.warning("bondization %s: %s", bond.secid, e)
            return bond

    def security_description(self, secid: str) -> dict:
        payload = self.fetch_json(f"/iss/securities/{secid}.json", {"iss.only": "description"}, ttl=86400)
        return {r.get("name"): r.get("value") for r in table(payload, "description")}

    # --- кривая ---
    def zcyc(self, on: Optional[date] = None) -> dict:
        params = {"iss.only": "yearyields"}
        if on:
            params["date"] = on.isoformat()
        return self.fetch_json("/iss/engines/stock/zcyc.json", params, ttl=3600)

    # --- история ---
    def history(self, secid: str, board: str, start: date, end: date, max_pages: int = 60) -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            payload = self.fetch_json(
                f"/iss/history/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json",
                {"from": start.isoformat(), "till": end.isoformat(), "start": offset,
                 "history.columns": "TRADEDATE,CLOSE,LEGALCLOSEPRICE,WAPRICE,YIELDCLOSE,YIELDATWAP,DURATION,ACCINT,FACEVALUE,VOLUME,VALUE"},
                ttl=6 * 3600,
            )
            rows = parse_history(payload)
            out.extend(rows)
            n = len((payload.get("history") or {}).get("data") or [])
            if n < 100:
                break
            offset += n
        return out

    def index_history(self, index: str, start: date, end: date, max_pages: int = 60) -> list[tuple[date, float]]:
        out: list[tuple[date, float]] = []
        offset = 0
        for _ in range(max_pages):
            payload = self.fetch_json(
                f"/iss/history/engines/stock/markets/index/boards/SNDX/securities/{index}.json",
                {"from": start.isoformat(), "till": end.isoformat(), "start": offset, "history.columns": "TRADEDATE,CLOSE"},
                ttl=6 * 3600,
            )
            rows = parse_index_history(payload)
            out.extend(rows)
            n = len((payload.get("history") or {}).get("data") or [])
            if n < 100:
                break
            offset += n
        return out

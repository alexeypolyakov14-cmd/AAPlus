"""Загрузка рыночного снимка: MOEX + ЦБ, либо офлайн из каталога фикстур."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .analytics.curve import ZeroCurve
from .config import Settings
from .data.cache import SqliteCache
from .data.cbr import KeyRateView, analyze_keyrate, fetch_keyrate_history, parse_keyrate_xml
from .data.moex import MoexClient, parse_board_securities
from .data.disclosure import EventsBook
from .data.financials import FinancialsBook, IssuerMap
from .data.news import NewsBook
from .data.ratings import RatingsBook
from .models import Bond, Quote
from .screener import build_curve

log = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    settle: date
    universe: list[tuple[Bond, Quote]]
    curve: Optional[ZeroCurve]
    keyrate: Optional[KeyRateView]
    keyrate_history: list[tuple[date, float]] = field(default_factory=list)
    enrich: Optional[Callable[[Bond], Bond]] = None
    source: str = "moex"
    ratings: Optional[RatingsBook] = None
    financials: Optional[FinancialsBook] = None
    issuers: Optional[IssuerMap] = None
    events: Optional[EventsBook] = None
    news: Optional[NewsBook] = None
    describe: Optional[Callable[[Bond], dict]] = None   # описание бумаги MOEX ISS (флаги дефолта)
    rejected: dict[str, str] = field(default_factory=dict)  # secid -> причина отсева последним скрином

    def screen_kwargs(self) -> dict:
        return {"enrich": self.enrich, "ratings": self.ratings, "financials": self.financials,
                "issuers": self.issuers, "events": self.events, "news": self.news, "describe": self.describe}


def load_ratings(settings: Settings) -> Optional[RatingsBook]:
    path = settings.get("data", "ratings_csv", default="")
    if not path:
        return None
    book = RatingsBook.from_csv(path, conservative=settings.get("data", "ratings_conservative", default=True),
                                policy=settings.get("data", "ratings_policy", default="primary"),
                                primary=settings.get("data", "ratings_primary", default=""),
                                fallback=settings.get("data", "ratings_fallback", default=None))
    return book if len(book) else None


def load_books(settings: Settings) -> tuple[Optional[FinancialsBook], Optional[IssuerMap], Optional[EventsBook], Optional[NewsBook]]:
    fin = FinancialsBook.from_csv(settings.get("data", "financials_csv", default=""))
    iss = IssuerMap.from_csv(settings.get("data", "issuers_csv", default=""))
    ev = EventsBook.from_csv(settings.get("data", "disclosure_csv", default=""))
    nw = NewsBook.from_csv(settings.get("data", "news_csv", default=""))
    return (fin if len(fin) else None), (iss if len(iss) else None), (ev if len(ev) else None), (nw if len(nw) else None)


def load_snapshot(settings: Settings, fixtures_dir: Optional[str] = None, client: Optional[MoexClient] = None) -> MarketSnapshot:
    ratings = load_ratings(settings)
    financials, issuers, events, news = load_books(settings)
    if fixtures_dir:
        snap = _load_fixtures(fixtures_dir)
        snap.ratings, snap.financials, snap.issuers, snap.events, snap.news = ratings, financials, issuers, events, news
        return snap
    cache = SqliteCache(settings.get("data", "cache_path", default="data/cache/http_cache.sqlite"))
    client = client or MoexClient(cache=cache)
    boards = settings.get("data", "boards", default=["TQOB", "TQCB"])
    today = date.today()
    universe = client.bonds(boards, today)
    log.info("MOEX: загружено %d облигаций с площадок %s", len(universe), ",".join(boards))
    zcyc = None
    try:
        zcyc = client.zcyc()
    except Exception as e:  # noqa: BLE001
        log.warning("zcyc недоступен, строим кривую по ОФЗ: %s", e)
    curve = None
    try:
        curve = build_curve(universe, today, zcyc)
    except ValueError as e:
        log.warning("кривая не построена: %s", e)
    kr_hist: list[tuple[date, float]] = []
    kr: Optional[KeyRateView] = None
    try:
        start = date.fromisoformat(settings.get("data", "keyrate_from", default="2013-09-13"))
        kr_hist = fetch_keyrate_history(max(start, today - timedelta(days=5 * 365)), today, cache=cache)
        if kr_hist and kr_hist[-1][0] < today:
            kr_hist.append((today, kr_hist[-1][1]))
        kr = analyze_keyrate(kr_hist) if kr_hist else None
    except Exception as e:  # noqa: BLE001
        log.warning("ключевая ставка ЦБ недоступна: %s", e)
    return MarketSnapshot(today, universe, curve, kr, kr_hist, enrich=client.enrich, source="moex", ratings=ratings,
                          financials=financials, issuers=issuers, events=events, news=news,
                          describe=lambda b: client.security_description(b.secid))


def _load_fixtures(d: str) -> MarketSnapshot:
    with open(os.path.join(d, "bonds_board.json"), encoding="utf-8") as f:
        board = json.load(f)
    settle = date.fromisoformat(board["marketdata"]["data"][0][-1][:10]) if board.get("marketdata", {}).get("data") else date.today()
    universe = parse_board_securities(board, settle)
    curve = None
    zp = os.path.join(d, "zcyc.json")
    if os.path.exists(zp):
        with open(zp) as f:
            curve = ZeroCurve.from_moex_zcyc(json.load(f))
    kr_hist, kr = [], None
    kp = os.path.join(d, "cbr_keyrate.xml")
    if os.path.exists(kp):
        with open(kp, encoding="utf-8") as f:
            kr_hist = parse_keyrate_xml(f.read())
        if kr_hist:
            if kr_hist[-1][0] < settle:
                kr_hist.append((settle, kr_hist[-1][1]))
            kr = analyze_keyrate([(dd, r) for dd, r in kr_hist if dd <= settle] or kr_hist)
    return MarketSnapshot(settle, universe, curve, kr, kr_hist, enrich=None, source=f"fixtures:{d}")

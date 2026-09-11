"""Центр раскрытия корпоративной информации (e-disclosure.ru): существенные факты эмитентов.

Что берём: дефолты и технические дефолты по купонам/погашению/оферте, реструктуризации, решения о купонах,
изменения рейтингов, публикацию отчётности. Всё складывается в data/disclosure.csv (книга событий), а скринер
использует книгу как стоп-фактор (эмитент с (тех)дефолтом за последние N дней отсекается).

Сайт защищён антиботом для зарубежных адресов и местами требует JavaScript, поэтому два режима загрузки:
  * requests (быстро; из РФ обычно достаточно);
  * Playwright/Chromium (pip install "bondtrader[browser]" && playwright install chromium) — если
    сервер отдаёт JS-челлендж. Включается флагом --browser или автоматически при 403/челлендже.

Страницы (могут меняться — есть команда `disclosure discover`):
  поиск:      /poisk-po-kompaniyam            (форма) и /api/search/companies?query=...   (JSON)
  компания:   /portal/company.aspx?id=<ID>
  факты:      /portal/event.aspx?EventId=<ID>  и лента /portal/events.aspx?company=<ID> (или вкладка на странице компании)
  RSS:        /rss/company/<ID> (не гарантирован)
"""
from __future__ import annotations

import csv
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import requests

from .ratings_web import HEADERS
from .tls import ru_ca_bundle

log = logging.getLogger(__name__)

BASE = "https://www.e-disclosure.ru"
SEARCH_API = BASE + "/api/search/companies?query={q}"
SEARCH_PAGE = BASE + "/poisk-po-kompaniyam"
COMPANY_PAGE = BASE + "/portal/company.aspx?id={id}"
EVENTS_PAGE = BASE + "/portal/events.aspx?company={id}&page={page}"
EVENT_PAGE = BASE + "/portal/event.aspx?EventId={id}"

# Классификация фактов по заголовку. Порядок важен: первое совпадение побеждает.
EVENT_TYPES: list[tuple[str, re.Pattern]] = [
    ("default", re.compile(r"неисполнени\w+ обязательств|дефолт(?!.*техническ)|невыплат|не исполнил|просрочк", re.I)),
    ("tech_default", re.compile(r"техническ\w+ дефолт", re.I)),
    ("restructuring", re.compile(r"реструктуризац|изменени\w+ услови\w+ (?:выпуска|исполнения)|общ\w+ собрани\w+ владельцев облигаций|представител\w+ владельцев", re.I)),
    ("rating", re.compile(r"рейтинг", re.I)),
    ("coupon", re.compile(r"(?:определени|установлени|размер)\w* (?:ставк|процентн)\w*.*купон|купонн\w+ доход", re.I)),
    ("offer", re.compile(r"оферт|приобретени\w+ (?:эмитентом )?облигаций|досрочн\w+ погашени", re.I)),
    ("redemption", re.compile(r"погашени\w+ облигаций|выплат\w+ .*погашени", re.I)),
    ("report", re.compile(r"бухгалтерск\w+ отчетност|финансов\w+ отчетност|мсфо|рсбу|консолидированн", re.I)),
    ("issue", re.compile(r"размещени\w+ (?:ценных бумаг|облигаций)|регистраци\w+ (?:выпуска|программы)|решени\w+ о выпуске", re.I)),
    ("corporate", re.compile(r"собрани\w+|совет\w* директоров|реорганизац|ликвидац|банкротств|иск|судебн", re.I)),
]
STOP_TYPES = {"default", "tech_default", "restructuring"}


@dataclass
class DisclosureEvent:
    date: date
    issuer: str
    kind: str
    title: str
    inn: str = ""
    company_id: str = ""
    url: str = ""


def classify(title: str) -> str:
    t = html.unescape(title or "")
    # «технический дефолт» проверяем раньше «дефолт», иначе всё уйдёт в default
    if EVENT_TYPES[1][1].search(t):
        return "tech_default"
    for kind, rx in EVENT_TYPES:
        if rx.search(t):
            return kind
    return "other"


# ---------------------------------------------------------------------------
# Загрузка страниц: requests, при необходимости — браузер
# ---------------------------------------------------------------------------

def looks_like_challenge(status: int, text: str) -> bool:
    if status in (403, 429, 503):
        return True
    low = text[:4000].lower()
    return any(k in low for k in ("challenge", "ddos-guard", "проверка браузера", "checking your browser", "captcha", "qrator"))


class EdisclosureClient:
    def __init__(self, browser: bool = False, timeout: float = 30, session: Optional[requests.Session] = None):
        self.browser = browser
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update(HEADERS)
        self.verify = ru_ca_bundle() or True
        self._pw = None

    # -- транспорт --
    def get(self, url: str, accept_json: bool = False) -> tuple[int, str]:
        if not self.browser:
            h = {"Accept": "application/json, text/plain, */*"} if accept_json else {}
            r = self.s.get(url, headers=h, timeout=self.timeout, verify=self.verify)
            if not looks_like_challenge(r.status_code, r.text):
                return r.status_code, r.text
            log.warning("%s: HTTP %d / антибот — пробую через браузер", url, r.status_code)
        return self.get_browser(url)

    def get_browser(self, url: str) -> tuple[int, str]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as e:
            raise RuntimeError("нужен Playwright: pip install 'bondtrader[browser]' && playwright install chromium") from e
        if self._pw is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page(user_agent=HEADERS["User-Agent"], locale="ru-RU")
        resp = self._page.goto(url, wait_until="networkidle", timeout=int(self.timeout * 1000))
        self._page.wait_for_timeout(1500)   # антибот часто делает редирект после проверки
        text = self._page.content()
        self.browser = True
        return (resp.status if resp else 200), text

    def close(self) -> None:
        if self._pw is not None:
            try:
                self._browser.close(); self._pw.stop()
            finally:
                self._pw = None

    # -- данные --
    def search(self, query: str) -> list[dict]:
        """Список компаний: [{id, name, inn}]. Сначала JSON-API, затем HTML-форма."""
        code, text = self.get(SEARCH_API.format(q=requests.utils.quote(query)), accept_json=True)
        items = parse_search_json(text) if code == 200 else []
        if items:
            return items
        code, text = self.get(SEARCH_PAGE + "?query=" + requests.utils.quote(query))
        return parse_search_html(text)

    def events(self, company_id: str, pages: int = 3, since: Optional[date] = None) -> list[DisclosureEvent]:
        out: list[DisclosureEvent] = []
        issuer = ""
        for page in range(1, pages + 1):
            code, text = self.get(EVENTS_PAGE.format(id=company_id, page=page))
            if code != 200:
                break
            if not issuer:
                m = re.search(r"<title>(.*?)</title>", text, re.S)
                issuer = html.unescape(m.group(1)).split(" - ")[0].strip() if m else ""
            evs = parse_events_html(text, issuer=issuer, company_id=company_id)
            if not evs:
                break
            out += evs
            if since and evs[-1].date < since:
                break
        if since:
            out = [e for e in out if e.date >= since]
        return out


# ---------------------------------------------------------------------------
# Парсеры (чистые функции, покрыты тестами на сохранённых фрагментах)
# ---------------------------------------------------------------------------

def parse_search_json(text: str) -> list[dict]:
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("items") or data.get("companies") or data.get("data") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cid = it.get("id") or it.get("companyId") or it.get("Id")
        name = it.get("name") or it.get("shortName") or it.get("Name") or ""
        if cid:
            out.append({"id": str(cid), "name": html.unescape(str(name)), "inn": str(it.get("inn") or it.get("INN") or "")})
    return out


def parse_search_html(text: str) -> list[dict]:
    out, seen = [], set()
    for m in re.finditer(r'href="[^"]*company\.aspx\?id=(\d+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        cid, name = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if cid in seen or not name:
            continue
        seen.add(cid)
        # ИНН обычно рядом в той же строке таблицы
        tail = text[m.end(): m.end() + 600]
        inn = re.search(r"\b(\d{10}|\d{12})\b", re.sub(r"<[^>]+>", " ", tail))
        out.append({"id": cid, "name": html.unescape(name), "inn": inn.group(1) if inn else ""})
    return out


_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def parse_events_html(text: str, issuer: str = "", company_id: str = "") -> list[DisclosureEvent]:
    """Лента фактов: строки с датой и ссылкой на event.aspx?EventId=... Терпимо к вёрстке: ищем пары
    (дата dd.mm.yyyy, ближайшая после неё ссылка на факт)."""
    out: list[DisclosureEvent] = []
    for m in re.finditer(r'href="([^"]*event\.aspx\?EventId=(\d+))"[^>]*>(.*?)</a>', text, re.S | re.I):
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3)))).strip()
        if not title:
            continue
        before = text[max(0, m.start() - 800): m.start()]
        dates = _DATE_RE.findall(before)
        if not dates:
            after = text[m.end(): m.end() + 300]
            dates = _DATE_RE.findall(after)
        if not dates:
            continue
        d, mo, y = dates[-1]
        try:
            dt = date(int(y), int(mo), int(d))
        except ValueError:
            continue
        url = m.group(1)
        if url.startswith("/"):
            url = BASE + url
        out.append(DisclosureEvent(dt, issuer, classify(title), title, company_id=company_id, url=url))
    return out


# ---------------------------------------------------------------------------
# Книга событий: data/disclosure.csv
# ---------------------------------------------------------------------------

class EventsBook:
    """Событии по эмитентам; поиск стоп-факторов по ИНН, id компании или названию."""

    def __init__(self, events: Iterable[DisclosureEvent] = ()):
        self.events: list[DisclosureEvent] = list(events)

    def __len__(self) -> int:
        return len(self.events)

    def add(self, e: DisclosureEvent) -> bool:
        key = (e.date, e.kind, e.title, e.company_id or e.inn or e.issuer)
        if any((x.date, x.kind, x.title, x.company_id or x.inn or x.issuer) == key for x in self.events):
            return False
        self.events.append(e)
        return True

    def for_issuer(self, inn: str = "", name: str = "", company_id: str = "") -> list[DisclosureEvent]:
        from .ratings import issuer_match
        out = []
        for e in self.events:
            if inn and e.inn == inn or company_id and e.company_id == company_id:
                out.append(e)
            elif name and e.issuer and issuer_match(e.issuer, name):
                out.append(e)
        return sorted(out, key=lambda e: e.date)

    def stop_factors(self, asof: date, days: int = 365, inn: str = "", name: str = "", company_id: str = "") -> list[DisclosureEvent]:
        since = asof - timedelta(days=days)
        return [e for e in self.for_issuer(inn, name, company_id) if e.kind in STOP_TYPES and since <= e.date <= asof]

    @classmethod
    def from_csv(cls, path: str) -> "EventsBook":
        book = cls()
        if not path or not os.path.exists(path):
            return book
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    d = date.fromisoformat((r.get("date") or "")[:10])
                except ValueError:
                    continue
                kind = (r.get("kind") or "").strip() or classify(r.get("title") or "")
                book.events.append(DisclosureEvent(d, (r.get("issuer") or "").strip(), kind, (r.get("title") or "").strip(),
                                                   (r.get("inn") or "").strip(), str(r.get("company_id") or "").strip(),
                                                   (r.get("url") or "").strip()))
        log.info("события e-disclosure: %d записей из %s", len(book), path)
        return book

    def to_csv(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "issuer", "inn", "company_id", "kind", "title", "url"])
            for e in sorted(self.events, key=lambda e: (e.date, e.issuer)):
                w.writerow([e.date.isoformat(), e.issuer, e.inn, e.company_id, e.kind, e.title, e.url])


def discover(query: str = "Балтийский лизинг", browser: bool = False) -> None:
    """Печатает, что отдаёт e-disclosure на поиск и ленту фактов первой найденной компании."""
    c = EdisclosureClient(browser=browser)
    try:
        for url, js in ((SEARCH_API.format(q=requests.utils.quote(query)), True),
                        (SEARCH_PAGE + "?query=" + requests.utils.quote(query), False)):
            try:
                code, text = c.get(url, accept_json=js)
            except Exception as e:  # noqa: BLE001
                print(f"== {url}\n   ERROR {e}")
                continue
            title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
            print(f"== {url}\n   HTTP {code} len={len(text)} title={title.group(1).strip()[:80] if title else '-'} "
                  f"challenge={looks_like_challenge(code, text)}")
            found = parse_search_json(text) if js else parse_search_html(text)
            print(f"   companies: {found[:5]}")
            print("   head: " + re.sub(r"\s+", " ", re.sub(r"<script.*?</script>", " ", text, flags=re.S)[:800]))
            if found:
                cid = found[0]["id"]
                for u in (COMPANY_PAGE.format(id=cid), EVENTS_PAGE.format(id=cid, page=1)):
                    code2, text2 = c.get(u)
                    evs = parse_events_html(text2, found[0]["name"], cid)
                    print(f"== {u}\n   HTTP {code2} len={len(text2)} events={len(evs)}")
                    for e in evs[:8]:
                        print(f"   {e.date} [{e.kind}] {e.title[:110]}")
                    links = sorted(set(re.findall(r'href="([^"]*(?:event|fact|rss|report|otchet)[^"]*)"', text2, re.I)))[:12]
                    print(f"   links: {links}")
                break
    finally:
        c.close()

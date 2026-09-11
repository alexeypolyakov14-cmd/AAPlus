"""ГИР БО ФНС (bo.nalog.ru): годовая бухгалтерская отчётность юрлиц по РСБУ.

Сайт — SPA, интерфейс которого ходит в недокументированный JSON-бэкенд:
  GET /nbo/organizations/search?query=<ИНН или название>&page=0&size=20   -> {"content":[{id, inn, shortName, ...}]}
  GET /advanced-search/organizations/search?query=...                      -> то же (второй вариант)
  GET /nbo/organizations/{orgId}/bfo/                                      -> [{period:"2024", correction:[{id,...}]}]
  GET /nbo/bfo/{bfoId}/details                                             -> [{balance:{current1600,...}, financialResult:{current2110,...}}]

Важно: из-за рубежа сайт отдаёт HTML-заглушку (геоблок), поэтому загрузка работает только из РФ.
Парсер терпим к деталям структуры: рекурсивно собирает все поля вида current1600 / previous2110.
Единицы измерения — по ОКЕИ (384 тыс. руб., 385 млн руб., 383 руб.); в Statement всё приводится к тыс. руб.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any, Iterable, Optional

import requests

from .financials import Statement
from .ratings_web import HEADERS
from .tls import ru_ca_bundle

log = logging.getLogger(__name__)

BASE = "https://bo.nalog.ru"
SEARCH_PATHS = ["/nbo/organizations/search?query={q}&page=0&size=20",
                "/advanced-search/organizations/search?query={q}&page=0&size=20"]
BFO_PATH = "/nbo/organizations/{org_id}/bfo/"
DETAILS_PATH = "/nbo/bfo/{bfo_id}/details"

_LINE_RE = re.compile(r"^(current|previous|beforePrevious)(\d{4})$")
OKEI_TO_THOUSANDS = {"384": 1.0, "385": 1000.0, "383": 0.001}


class GirboUnavailable(RuntimeError):
    """ГИР БО отдал не JSON (геоблок / заглушка SPA / антибот)."""


class GirboClient:
    def __init__(self, timeout: float = 30, session: Optional[requests.Session] = None, base: str = BASE):
        self.base = base
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update(HEADERS)
        self.s.headers.update({"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"})
        self.verify = ru_ca_bundle() or True

    def get_json(self, path: str) -> Any:
        url = self.base + path
        r = self.s.get(url, timeout=self.timeout, verify=self.verify)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "json" not in ctype:
            head = re.sub(r"\s+", " ", r.text[:160])
            raise GirboUnavailable(f"{url}: HTTP {r.status_code} {ctype or '-'} — ГИР БО доступен только из РФ; ответ: {head!r}")
        return r.json()

    # ---- организации ----
    def search(self, query: str) -> list[dict]:
        last: Optional[Exception] = None
        for tpl in SEARCH_PATHS:
            try:
                data = self.get_json(tpl.format(q=requests.utils.quote(query)))
            except GirboUnavailable as e:
                last = e
                continue
            items = data.get("content") if isinstance(data, dict) else data
            if isinstance(items, list):
                return items
        if last:
            raise last
        return []

    def find_org(self, query: str) -> Optional[dict]:
        """ИНН — точное совпадение; название — первый результат."""
        items = self.search(query)
        if not items:
            return None
        if query.isdigit():
            for it in items:
                if str(it.get("inn") or "") == query:
                    return it
            return None
        return items[0]

    # ---- отчётность ----
    def bfo_list(self, org_id: Any) -> list[dict]:
        data = self.get_json(BFO_PATH.format(org_id=org_id))
        return data if isinstance(data, list) else data.get("content", [])

    def details(self, bfo_id: Any) -> Any:
        return self.get_json(DETAILS_PATH.format(bfo_id=bfo_id))

    def statements(self, org: dict, years: Optional[Iterable[int]] = None) -> list[Statement]:
        inn = str(org.get("inn") or "")
        out: list[Statement] = []
        wanted = set(years) if years else None
        for item in self.bfo_list(org.get("id")):
            year = _year_of(item)
            if year is None or (wanted and year not in wanted):
                continue
            # берём последнюю корректировку отчётности за период
            corrections = item.get("correction") or item.get("corrections") or []
            cand_ids = [c.get("id") for c in corrections if isinstance(c, dict) and c.get("id") is not None]
            bfo_id = cand_ids[-1] if cand_ids else item.get("id")
            if bfo_id is None:
                continue
            try:
                payload = self.details(bfo_id)
            except GirboUnavailable as e:
                log.warning("ИНН %s, %d: %s", inn, year, e)
                continue
            st = parse_details(payload, inn, year)
            if st and st.values:
                out.append(st)
        out.sort(key=lambda s: s.year)
        return out


def _year_of(item: dict) -> Optional[int]:
    for k in ("period", "year", "reportPeriod", "periodYear"):
        v = item.get(k)
        if v is None:
            continue
        m = re.search(r"(20\d{2})", str(v))
        if m:
            return int(m.group(1))
    return None


def parse_details(payload: Any, inn: str, year: int) -> Optional[Statement]:
    """Собирает коды строк из ответа details (список или словарь произвольной вложенности)."""
    values: dict[str, float] = {}
    prev: dict[str, float] = {}
    okei: Optional[str] = None

    def walk(node: Any) -> None:
        nonlocal okei
        if isinstance(node, dict):
            if okei is None and node.get("okei") is not None:
                okei = str(node["okei"])
            for k, v in node.items():
                m = _LINE_RE.match(str(k))
                if m and isinstance(v, (int, float)) and not isinstance(v, bool):
                    (values if m.group(1) == "current" else prev if m.group(1) == "previous" else {})[m.group(2)] = float(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(payload)
    if not values:
        return None
    k = OKEI_TO_THOUSANDS.get(okei or "384", 1.0)
    st = Statement(inn=inn, year=year, values={c: v * k for c, v in values.items()}, source="girbo")
    st.previous = {c: v * k for c, v in prev.items()}
    return st


# ---------------------------------------------------------------------------
# Кэш на диске: data/financials/<ИНН>.json  (сырые Statement по годам)
# ---------------------------------------------------------------------------

def cache_path(cache_dir: str, inn: str) -> str:
    return os.path.join(cache_dir, f"{inn}.json")


def save_cache(cache_dir: str, inn: str, org: dict, statements: list[Statement]) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    p = cache_path(cache_dir, inn)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"inn": inn, "name": org.get("shortName") or org.get("fullName") or "", "org_id": org.get("id"),
                   "fetched": date.today().isoformat(),
                   "statements": [{"year": s.year, "values": s.values, "previous": getattr(s, "previous", {}), "source": s.source}
                                  for s in statements]}, f, ensure_ascii=False, indent=1)
    return p


def load_cache(cache_dir: str, inn: str) -> Optional[dict]:
    p = cache_path(cache_dir, inn)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fetch_issuer(client: GirboClient, query: str, cache_dir: str = "data/financials", years: int = 3,
                 refresh: bool = False) -> tuple[Optional[dict], list[Statement]]:
    """Отчётность по ИНН или названию: из кэша, иначе с ГИР БО (с записью в кэш)."""
    if query.isdigit() and not refresh:
        cached = load_cache(cache_dir, query)
        if cached:
            sts = [Statement(query, s["year"], {k: float(v) for k, v in s["values"].items()}, s.get("source", "girbo")) for s in cached["statements"]]
            return {"inn": query, "shortName": cached.get("name"), "id": cached.get("org_id")}, sts
    org = client.find_org(query)
    if not org:
        return None, []
    inn = str(org.get("inn") or "")
    this_year = date.today().year
    sts = client.statements(org, years=range(this_year - years - 1, this_year + 1))
    if inn:
        save_cache(cache_dir, inn, org, sts)
    return org, sts

"""Разведка и загрузка рейтингов с сайтов АКРА и Эксперт РА.

У агентств нет открытого API; страницы могут меняться. Модуль устроен так:
  * discover() — скачивает страницы-кандидаты и печатает их структуру (заголовок, найденные
    JSON-эндпоинты, фрагменты таблиц), чтобы адаптировать парсеры без доступа к сети из IDE;
  * parse_*_html() — чистые функции над текстом, покрываются тестами на сохранённых фрагментах.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date
from typing import Optional

import requests

from .ratings import Rating, normalize_rating
from .tls import ru_ca_bundle

log = logging.getLogger(__name__)

CANDIDATES = {
    "acra_issuers": "https://www.acra-ratings.ru/ratings/issuers/",
    "acra_issues": "https://www.acra-ratings.ru/ratings/issues/",
    "raexpert_all": "https://raexpert.ru/ratings/",
    "raexpert_credits": "https://raexpert.ru/ratings/credits/",
    "raexpert_bankcredits": "https://raexpert.ru/ratings/bankcredits/",
    "nkr_press": "https://ratings.ru/ratings/",
    "nkr_issuers": "https://ratings.ru/ratings/issuers/",
    "nkr_issues": "https://ratings.ru/ratings/issues/",
    "nra": "https://www.ra-national.ru/ratings/",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "ru,en;q=0.8"}


def fetch(url: str, timeout: float = 25) -> tuple[int, str, str]:
    verify = ru_ca_bundle() or True
    r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
    return r.status_code, r.headers.get("Content-Type", ""), r.text


def discover(names: Optional[list[str]] = None, sample: int = 1200) -> None:
    for name, url in CANDIDATES.items():
        if names and name not in names:
            continue
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== {name} {url}\n   ERROR {e}")
            continue
        title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        apis = sorted(set(re.findall(r"""["'](/api/[^"' ]{3,120})["']""", text)))[:20]
        tables = re.findall(r"<table.*?</table>", text, re.S | re.I)
        rating_hits = re.findall(r"(?:ru)?[ABC]{1,3}[+-]?(?:\(RU\)|\.ru)", text)[:15]
        scripts = sorted(set(re.findall(r"""<script[^>]+src=["']([^"']+)["']""", text)))[:15]
        links = sorted(set(re.findall(r"""["'](https?://[^"' ]*rat[^"' ]{0,80}|/[^"' ]*(?:rating|rate|ajax|json|export|xls)[^"' ]{0,80})["']""", text, re.I)))[:25]
        print(f"== {name} {url}\n   HTTP {code} {ctype} len={len(text)} title={title.group(1).strip()[:100] if title else '-'}")
        print(f"   tables={len(tables)} api_paths={apis}")
        print(f"   scripts={scripts}")
        print(f"   links={links}")
        print(f"   rating-like tokens: {rating_hits}")
        for i, t in enumerate(tables[:2]):
            head = re.sub(r"\s+", " ", t)[:700]
            print(f"   table[{i}] head: {head}")
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", html.unescape(body))
        i = body.lower().find("рейтинг")
        print("   text sample: " + body[max(i - 200, 0): max(i - 200, 0) + sample])


# ---------------------------------------------------------------------------
# Парсеры (заполняются после discover)
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def _cells(row_html: str) -> list[str]:
    out = []
    for c in _CELL_RE.findall(row_html):
        t = re.sub(r"<[^>]+>", " ", c)
        out.append(re.sub(r"\s+", " ", html.unescape(t)).strip())
    return out


def parse_generic_table(text: str, agency: str, kind: str = "issuer") -> list[Rating]:
    """Универсальный разбор HTML-таблиц: строка = (название, ..., рейтинг, ..., дата)."""
    out: list[Rating] = []
    for row in _ROW_RE.findall(text):
        cells = _cells(row)
        if len(cells) < 2:
            continue
        rating = None
        for c in cells:
            r = normalize_rating(c)
            if r and len(c) <= 12:
                rating = r
                break
        if not rating:
            continue
        subject = cells[0]
        d = None
        for c in cells:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", c)
            if m:
                d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                break
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", c)
            if m:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                break
        isin = next((c for c in cells if re.fullmatch(r"RU000[A-Z0-9]{7}", c)), "")
        out.append(Rating(subject=subject, agency=agency, rating=rating, date=d, kind=kind, isin=isin))
    return out


# ---- НКР: пресс-релизы (HTML-таблица: заголовок, сектор, дата) ----
_NKR_TITLE_RE = re.compile(
    r"НКР\s+(?P<action>присвоило|подтвердило|повысило|снизило|изменило|отозвало)\s+(?P<body>.+)", re.I | re.S)
_NKR_RATING_RE = re.compile(r"(?:до|на уровне|уровне|рейтинг)\s+(?P<r>[ABC]{1,3}[+-]?)\.ru", re.I)
_NKR_ANY_RATING_RE = re.compile(r"(?P<r>[ABC]{1,3}[+-]?)\.ru")
_QUOTED_RE = re.compile(r"[«\"]([^»\"]+)[»\"]")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def parse_nkr_press(text: str) -> list[Rating]:
    """Пресс-релизы НКР -> рейтинги. Для «снизило ... с X до Y» берётся Y; отозванные пропускаются."""
    out: list[Rating] = []
    for row in _ROW_RE.findall(text):
        cells = _cells(row)
        if not cells:
            continue
        title = cells[0]
        m = _NKR_TITLE_RE.search(title)
        if not m or m.group("action").lower() == "отозвало":
            continue
        body = m.group("body")
        rm = None
        if "до " in body:
            rm = re.search(r"до\s+(?P<r>[ABC]{1,3}[+-]?)\.ru", body, re.I)
        rm = rm or _NKR_RATING_RE.search(body) or _NKR_ANY_RATING_RE.search(body)
        if not rm:
            continue
        rating = normalize_rating(rm.group("r"))
        if not rating:
            continue
        q = _QUOTED_RE.search(body)
        subject = q.group(1).strip() if q else body.split(" кредитный")[0].strip()
        kind = "issue" if re.search(r"выпуск|облигац", body, re.I) else "issuer"
        d = None
        for c in cells[1:]:
            dm = _DATE_RE.search(c)
            if dm:
                d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                break
        out.append(Rating(subject=subject, agency="НКР", rating=rating, date=d, kind=kind))
    return out


def load_nkr(pages: int = 3) -> list[Rating]:
    """Загружает последние страницы пресс-релизов НКР."""
    out: list[Rating] = []
    for page in range(1, pages + 1):
        url = CANDIDATES["nkr_press"] + (f"?PAGEN_1={page}" if page > 1 else "")
        try:
            code, _, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("НКР %s: %s", url, e)
            break
        if code != 200:
            break
        got = parse_nkr_press(text)
        if not got:
            break
        out.extend(got)
    return out

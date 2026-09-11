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
    "acra_emissions": "https://www.acra-ratings.ru/ratings/emissions/",
    "raexpert_credits_all": "https://raexpert.ru/ratings/credits_all/",
    "raexpert_bankcredit_all": "https://raexpert.ru/ratings/bankcredit_all/",
    "raexpert_debt_inst": "https://raexpert.ru/ratings/debt_inst/",
    "raexpert_credits_fin": "https://raexpert.ru/ratings/credits_fin/",
    "raexpert_credits_holding": "https://raexpert.ru/ratings/credits_holding/",
    "raexpert_export": "https://raexpert.ru/all-services/rating-export",
    "nkr_press": "https://ratings.ru/ratings/",
    "nkr_issuers": "https://ratings.ru/ratings/issuers/",
    "nkr_issues": "https://ratings.ru/ratings/issues/",
    "nra": "https://www.ra-national.ru/ratings/",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "ru,en;q=0.8"}


def fetch(url: str, timeout: float = 25, insecure_fallback: bool = True) -> tuple[int, str, str]:
    """GET с бандлом Минцифры. Для публичных справочных данных при ошибке TLS допускается повтор без проверки
    сертификата (с предупреждением): риск подмены рейтингов ниже, чем польза от их наличия."""
    verify = ru_ca_bundle() or True
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
    except requests.exceptions.SSLError as e:
        if not insecure_fallback:
            raise
        log.warning("%s: TLS не проверен (%s) — повтор без проверки сертификата", url, str(e)[:120])
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
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


def discover_deep(urls: Optional[list[str]] = None) -> None:
    """Формы, пагинация и ajax-подсказки на страницах агентств."""
    urls = urls or ["https://raexpert.ru/ratings/credits_all/", "https://raexpert.ru/all-services/rating-export",
                    "https://www.acra-ratings.ru/ratings/issuers/", "https://www.acra-ratings.ru/ratings/emissions/"]
    for url in urls:
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== {url}\n   ERROR {e}")
            continue
        print(f"== {url}  HTTP {code} len={len(text)}")
        for m in re.finditer(r"<form[^>]*>(.*?)</form>", text, re.S | re.I):
            tag = re.search(r"<form[^>]*>", m.group(0), re.I).group(0)
            inputs = re.findall(r"""<(?:input|select|textarea)[^>]*name=["']([^"']+)["'][^>]*""", m.group(1), re.I)
            print(f"   FORM {tag[:200]} inputs={inputs[:25]}")
        pag = sorted(set(re.findall(r"""href=["']([^"']*(?:page|PAGEN|offset|start)=[^"']*)["']""", text, re.I)))[:15]
        print(f"   pagination hrefs: {pag}")
        ajax = sorted(set(re.findall(r"""["']([^"']*(?:ajax|\.php|api/|json)[^"']{0,100})["']""", text, re.I)))[:30]
        print(f"   ajax-like: {ajax}")
        data_attrs = sorted(set(re.findall(r"""data-(?:url|src|ajax|action|load)=["']([^"']+)["']""", text, re.I)))[:15]
        print(f"   data-url attrs: {data_attrs}")
        rows = len(re.findall(r"<tr", text, re.I))
        print(f"   <tr> count: {rows}; 'Показать ещё'/'load more' hits: {len(re.findall(r'(?i)показать ещ|load more|ещё', text))}")
        for m in list(re.finditer(r"(?i)pagination|paginat|b-pager|pager", text))[:3]:
            i = m.start()
            print("   near pager: " + re.sub(r"\s+", " ", text[max(0, i - 300): i + 500])[:800])


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


# ---- НКР: таблицы «Эмитенты» и «Эмиссии» ----
_ISIN_RE = re.compile(r"\b(RU000[A-Z0-9]{7})\b")


def _header_map(text: str) -> dict[str, int]:
    """Индексы колонок по заголовку таблицы."""
    m = re.search(r"<thead.*?</thead>", text, re.S | re.I)
    head = m.group(0) if m else (re.search(r"<tr.*?</tr>", text, re.S | re.I) or re.search(r"$", text)).group(0)
    cols = [c.lower() for c in _cells(head)]
    idx: dict[str, int] = {}
    for i, c in enumerate(cols):
        if "рейтингуемое" in c or c.startswith("объект") or (c.startswith("наименование") and "эмисси" not in c):
            idx.setdefault("subject", i)
        elif "эмисси" in c:
            idx["issue"] = i
            idx.setdefault("subject", i)   # эмитент извлекается из ссылки внутри ячейки
        elif c.startswith("рейтинг") and "esg" not in c:
            idx.setdefault("rating", i)
        elif c == "isin":
            idx["isin"] = i
        elif c.startswith("дата") or c.startswith("обновл"):
            idx["date"] = i
        elif "прогноз" in c:
            idx["outlook"] = i
    return idx


_COMPANY_LINK_RE = re.compile(r"""<a[^>]+href=["'][^"']*/database/companies/[^"']*["'][^>]*>(.*?)</a>""", re.S | re.I)
_ISSUER_LINK_RE = re.compile(r"""<a[^>]+href=["'][^"']*/ratings/issuers/[^"']*["'][^>]*>(.*?)</a>""", re.S | re.I)


def _issuer_from_cell(cell_html: str) -> str:
    for rx in (_COMPANY_LINK_RE, _ISSUER_LINK_RE):
        m = rx.search(cell_html)
        if m:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
    return ""


def parse_table_with_header(text: str, agency: str, kind: str) -> list[Rating]:
    idx = _header_map(text)
    if "rating" not in idx or "subject" not in idx:
        return parse_generic_table(text, agency, kind)
    out: list[Rating] = []
    body = re.search(r"<tbody.*?</tbody>", text, re.S | re.I)
    for row in _ROW_RE.findall(body.group(0) if body else text):
        cells = _cells(row)
        raw_cells = _CELL_RE.findall(row)
        if len(cells) <= max(idx["rating"], idx["subject"]):
            continue
        rating = normalize_rating(cells[idx["rating"]])
        if not rating:
            continue
        subject = cells[idx["subject"]]
        if "issue" in idx and idx["issue"] < len(raw_cells):
            issuer = _issuer_from_cell(raw_cells[idx["issue"]])
            if issuer:
                subject = issuer
        elif idx["subject"] < len(raw_cells):
            issuer = _issuer_from_cell(raw_cells[idx["subject"]])
            if issuer:
                subject = issuer
        isin = ""
        if "isin" in idx and idx["isin"] < len(cells):
            m = _ISIN_RE.search(cells[idx["isin"]])
            isin = m.group(1) if m else ""
        if not isin and "issue" in idx and idx["issue"] < len(cells):
            m = _ISIN_RE.search(cells[idx["issue"]])
            isin = m.group(1) if m else ""
        d = None
        if "date" in idx and idx["date"] < len(cells):
            dm = _DATE_RE.search(cells[idx["date"]])
            if dm:
                d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        out.append(Rating(subject=subject, agency=agency, rating=rating, date=d, kind=kind, isin=isin))
    return out


def load_nkr_tables() -> list[Rating]:
    out: list[Rating] = []
    for key, kind in (("nkr_issuers", "issuer"), ("nkr_issues", "issue")):
        try:
            code, _, text = fetch(CANDIDATES[key])
        except Exception as e:  # noqa: BLE001
            log.warning("НКР %s: %s", key, e)
            continue
        if code == 200:
            got = parse_table_with_header(text, "НКР", kind)
            log.info("НКР %s: %d записей", key, len(got))
            out.extend(got)
    return out


# ---- Эксперт РА: списки рейтингов по разделам (HTML-таблицы, пагинация ?page=N) ----
RAEXPERT_SECTIONS = {
    "credits_all": ("issuer", "https://raexpert.ru/ratings/credits_all/"),
    "credits_fin": ("issuer", "https://raexpert.ru/ratings/credits_fin/"),
    "credits_holding": ("issuer", "https://raexpert.ru/ratings/credits_holding/"),
    "credits_project": ("issuer", "https://raexpert.ru/ratings/credits_project/"),
    "bankcredit_all": ("issuer", "https://raexpert.ru/ratings/bankcredit_all/"),
    "leasing_rel": ("issuer", "https://raexpert.ru/ratings/leasing_rel/"),
    "mfi_credits_all": ("issuer", "https://raexpert.ru/ratings/mfi_credits_all/"),
    "debt_inst": ("issue", "https://raexpert.ru/ratings/debt_inst/"),
}


def load_raexpert(max_pages: int = 60) -> list[Rating]:
    out: list[Rating] = []
    for key, (kind, url) in RAEXPERT_SECTIONS.items():
        seen: set[tuple] = set()
        n_section = 0
        scheme = None  # какая схема пагинации сработала: ?page=, ?PAGEN_1=, ?p=
        for page in range(1, max_pages + 1):
            candidates = [url] if page == 1 else ([f"{url}?{scheme}={page}"] if scheme else
                                                  [f"{url}?page={page}", f"{url}?PAGEN_1={page}", f"{url}?p={page}"])
            new: list[Rating] = []
            for u in candidates:
                try:
                    code, _, text = fetch(u)
                except Exception as e:  # noqa: BLE001
                    log.warning("Эксперт РА %s: %s", u, e)
                    continue
                if code != 200:
                    continue
                got = parse_table_with_header(text, "Эксперт РА", kind)
                new = [r for r in got if (r.subject, r.rating, r.date) not in seen]
                if new:
                    if page > 1 and not scheme:
                        scheme = u.split("?")[1].split("=")[0]
                    break
            if not new:
                break
            for r in new:
                seen.add((r.subject, r.rating, r.date))
            out.extend(new)
            n_section += len(new)
        log.info("Эксперт РА %s: %d записей", key, n_section)
    return out

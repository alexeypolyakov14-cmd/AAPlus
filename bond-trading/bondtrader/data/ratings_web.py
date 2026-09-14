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
    "acra_press": "https://www.acra-ratings.ru/press-releases/",
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
    urls = urls or ["https://www.acra-ratings.ru/press-releases/", "https://www.acra-ratings.ru/ratings/issuers/?ajax=y",
                    "https://raexpert.ru/ratings/regions/", "https://raexpert.ru/ratings/municipal/", "https://raexpert.ru/ratings/credits_by/"]
    for url in urls:
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== {url}\n   ERROR {e}")
            continue
        print(f"== {url}  HTTP {code} len={len(text)}")
        print(f"   rating-like tokens: {re.findall(r'(?:ru)?[ABC]{1,3}[+-]?(?:[(]RU[)]|[.]ru)', text)[:12]}; <table>: {len(re.findall(r'<table', text, re.I))}")
        for m in list(re.finditer(r"(?:ru)?[ABC]{1,3}[+-]?(?:[(]RU[)]|[.]ru)", text))[:2]:
            i = m.start()
            print("   near rating: " + re.sub(r"\s+", " ", text[max(0, i - 700): i + 300])[:1000])
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
        for m in list(re.finditer(r"(?i)pagination|paginat|b-pager|pager", text))[:1]:
            i = m.start()
            print("   near pager: " + re.sub(r"\s+", " ", text[max(0, i - 300): i + 500])[:800])
        # тела JS-функций и переменных, отвечающих за пагинацию/экспорт
        for key in ("Выполняет аякс запрос", "PAGEN_1", "documents-row__item", "data-type=\"date\"", "load-more", "showMore"):
            for m in list(re.finditer(re.escape(key), text))[:2]:
                i = m.start()
                print(f"   JS[{key}]: " + re.sub(r"\s+", " ", text[max(0, i - 200): i + 900]))
        forms_full = re.findall(r"<form[^>]*>", text, re.I)
        print("   form tags: " + " | ".join(re.sub(r"\s+", " ", f)[:220] for f in forms_full[:12]))


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


_COMPANY_HREF_RE = re.compile(r"""href=["']([^"']*/database/companies/[^"']*)["']""", re.I)
_NKR_ISSUER_HREF_RE = re.compile(r"""href=["']([^"']*/ratings/issuers/[^"'#?]+)["']""", re.I)
NKR_BASE = "https://ratings.ru"


def _company_url_from_cell(cell_html: str) -> str:
    """Ссылка на страницу компании у агентства (там список пресс-релизов с метриками), абсолютная:
    Эксперт РА — /database/companies/…, НКР — /ratings/issuers/<slug>/."""
    for rx, base in ((_COMPANY_HREF_RE, RAEXPERT_BASE), (_NKR_ISSUER_HREF_RE, NKR_BASE)):
        m = rx.search(cell_html)
        if m:
            href = html.unescape(m.group(1))
            return href if href.startswith("http") else base + href
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
        url = ""
        if "issue" in idx and idx["issue"] < len(raw_cells):
            issuer = _issuer_from_cell(raw_cells[idx["issue"]])
            url = _company_url_from_cell(raw_cells[idx["issue"]])
            if issuer:
                subject = issuer
        elif idx["subject"] < len(raw_cells):
            issuer = _issuer_from_cell(raw_cells[idx["subject"]])
            url = _company_url_from_cell(raw_cells[idx["subject"]])
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
        out.append(Rating(subject=subject, agency=agency, rating=rating, date=d, kind=kind, isin=isin, url=url))
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
    "regions": ("issuer", "https://raexpert.ru/ratings/regions/"),
}


_HASH_RE = re.compile(r"setRatingPageHash\('([^']+)'\)")
_CSRF_RE = re.compile(r"CSRFAjaxTokenPageHash\s*=\s*'([^']+)'")
RAEXPERT_BASE = "https://raexpert.ru"


def decode_page_hash(h: str) -> dict:
    """'…PAGE:2' — base64 с первыми 10 символами, перенесёнными в конец. Возвращает {TIME, RATING_ID, PAGE}."""
    import base64
    try:
        raw = base64.b64decode(h[-10:] + h[:-10] + "==").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for part in raw.split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def encode_page_hash(rating_id: str, page: int, ts: Optional[int] = None) -> str:
    import base64
    import time
    raw = f"TIME:{ts or int(time.time())}|RATING_ID:{rating_id}|PAGE:{page}"
    b = base64.b64encode(raw.encode()).decode().rstrip("=")
    # обратная перестановка: первые 10 символов base64 уходят в конец
    return b[10:] + b[:10]


class RaexpertClient:
    """Обход рейтинг-листов Эксперт РА с серверной пагинацией через cookie."""

    def __init__(self, session: Optional[requests.Session] = None, get=None, post=None):
        self.s = session or requests.Session()
        self.s.headers.update(HEADERS)
        self._get = get
        self._post = post
        self.verify = ru_ca_bundle() or True

    def get(self, url: str) -> str:
        if self._get:
            return self._get(url)
        r = self.s.get(url, timeout=25, verify=self.verify)
        r.raise_for_status()
        return r.text

    def set_page(self, page_hash: str, csrf: str) -> None:
        if self._post:
            self._post(page_hash, csrf)
            return
        self.s.post(RAEXPERT_BASE + "/ratings/index/ajax-set-rating-page-hash/",
                    data={"rating_page_hash": page_hash, "CSRFAjaxToken": csrf}, timeout=25, verify=self.verify,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": RAEXPERT_BASE + "/ratings/"})

    def load_section(self, url: str, kind: str, max_pages: int = 80) -> list[Rating]:
        out: list[Rating] = []
        seen_rows: set[tuple] = set()
        text = self.get(url)
        rating_id = None
        visited: set[int] = {1}
        queue: list[tuple[int, str]] = []

        def absorb(text: str) -> int:
            nonlocal rating_id
            got = parse_table_with_header(text, "Эксперт РА", kind)
            n = 0
            for r in got:
                key = (r.subject, r.rating, r.date, r.kind)
                if key not in seen_rows:
                    seen_rows.add(key)
                    out.append(r)
                    n += 1
            for h in _HASH_RE.findall(text):
                info = decode_page_hash(h)
                pg = int(info.get("PAGE", 0) or 0)
                rating_id = rating_id or info.get("RATING_ID")
                if pg and pg not in visited and all(pg != q[0] for q in queue):
                    queue.append((pg, h))
            return n

        absorb(text)
        pages_done = 1
        while pages_done < max_pages:
            if not queue:
                # пагинатор показывает не все страницы — генерируем следующую
                if not rating_id:
                    break
                nxt = max(visited) + 1
                queue.append((nxt, encode_page_hash(rating_id, nxt)))
            queue.sort()
            pg, h = queue.pop(0)
            if pg in visited:
                continue
            m = _CSRF_RE.search(text)
            if not m:
                break
            visited.add(pg)
            self.set_page(h, m.group(1))
            text = self.get(url)
            n_new = absorb(text)
            pages_done += 1
            if n_new == 0:
                break
        log.info("Эксперт РА %s: страниц %d, последняя %d", url, pages_done, max(visited))
        return out


def load_raexpert(max_pages: int = 80, client: Optional[RaexpertClient] = None) -> list[Rating]:
    client = client or RaexpertClient()
    out: list[Rating] = []
    for key, (kind, url) in RAEXPERT_SECTIONS.items():
        try:
            got = client.load_section(url, kind, max_pages=max_pages)
        except Exception as e:  # noqa: BLE001
            log.warning("Эксперт РА %s: %s", key, e)
            continue
        log.info("Эксперт РА %s: %d записей", key, len(got))
        out.extend(got)
    return out


# ---- АКРА: пресс-релизы (серверный HTML, блоки documents-row) ----
_ACRA_ITEM_RE = re.compile(r'<span class="item__emit">(.*?)</span>.*?<a class="item__title"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_ACRA_LEVEL_RE = re.compile(r"(?:ДО УРОВНЯ|НА УРОВНЕ|УРОВНЯ|РЕЙТИНГ)\s+([ABC]{1,3}[+-]?)\(RU\)", re.I)
_ACRA_ANY_RE = re.compile(r"([ABC]{1,3}[+-]?)\(RU\)")


def parse_acra_press(text: str, withdrawn: Optional[set] = None) -> list[Rating]:
    """Пресс-релизы АКРА -> рейтинги. Отзывы пропускаются и (если передан withdrawn) запоминаются;
    релизы идут от новых к старым, поэтому рейтинг субъекта, отозванный позже, не учитывается."""
    out: list[Rating] = []
    # дата ищется в окрестности элемента (после заголовка)
    for m in _ACRA_ITEM_RE.finditer(text):
        emit = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(3)))).strip()
        up = title.upper()
        if "ОТОЗВАЛО" in up or "ОТЗЫВ" in up or "ПРЕКРАТИЛО" in up:
            if withdrawn is not None and "ВЫПУСК" not in up and "ОБЛИГАЦ" not in up:
                withdrawn.add(emit or title)
            continue
        if withdrawn and (emit or title) in withdrawn and not re.search(r"ВЫПУСК|ОБЛИГАЦ", up):
            continue
        rm = _ACRA_LEVEL_RE.search(title) or _ACRA_ANY_RE.search(title)
        if not rm:
            continue
        rating = normalize_rating(rm.group(1))
        if not rating:
            continue
        kind = "issue" if re.search(r"ВЫПУСК|ОБЛИГАЦ", up) else "issuer"
        tail = text[m.end(): m.end() + 1500]
        dm = _DATE_RE.search(tail)
        d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))) if dm else None
        out.append(Rating(subject=emit or title, agency="АКРА", rating=rating, date=d, kind=kind))
    return out


def load_acra_press(max_pages: int = 120) -> list[Rating]:
    """Обход пресс-релизов АКРА: ?PAGEN_1=N (Bitrix), ~30 релизов на страницу, 120 страниц ≈ год.
    Останавливается, когда новых записей нет."""
    out: list[Rating] = []
    seen: set[tuple] = set()
    seen_links: set[str] = set()
    withdrawn: set[str] = set()
    for page in range(1, max_pages + 1):
        url = CANDIDATES["acra_press"] + (f"?PAGEN_1={page}" if page > 1 else "")
        try:
            code, _, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("АКРА %s: %s", url, e)
            break
        if code != 200:
            break
        # конец списка — когда на странице нет ни одного нового релиза (рейтинговых действий там лишь ~10–15%)
        links = set(re.findall(r'class="item__title"[^>]*href="([^"]+)"', text))
        if not links or links <= seen_links:
            break
        seen_links |= links
        got = parse_acra_press(text, withdrawn)
        new = [r for r in got if (r.subject, r.rating, r.date, r.kind) not in seen]
        for r in new:
            seen.add((r.subject, r.rating, r.date, r.kind))
        out.extend(new)
    log.info("АКРА пресс-релизы: %d записей", len(out))
    return out

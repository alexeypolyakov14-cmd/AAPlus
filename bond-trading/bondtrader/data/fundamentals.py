"""Фундаментальные данные по эмитенту из того, что доступно из CI (без ГИР БО и e-disclosure, они геоблокированы).

Три источника:
  • карта публичного долга с MOEX: все выпуски эмитента, объём в обращении, купон, погашение/оферта/амортизация,
    график погашений по годам (стена рефинансирования видна без отчётности);
  • пресс-релиз рейтингового агентства: у Эксперт РА страница компании в базе ведёт на релизы, где в тексте
    есть долг/EBITDA, покрытие процентов, ликвидность, выручка. Достаём предложения с этими словами;
  • ГИР БО — пробуем честно; из-за рубежа сайт отдаёт заглушку, тогда так и говорим.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..models import Bond, Quote

log = logging.getLogger(__name__)

# Эксперт РА: /releases/2026/sep10a; НКР: /ratings/press-releases/Polyplast-RA-101125/
_RELEASE_HREF_RE = re.compile(r"""href=["']([^"']*(?:/releases/\d{4}/[^"'#?]+|/ratings/press-releases/[^"'#?/]+/?))["']""", re.I)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>|<title>(.*?)</title>", re.S | re.I)
_DATE_RE = re.compile(r"(\d{1,2})[./](\d{2})[./](\d{4})")
KEYWORDS = ("долг", "ebitda", "oibda", "ffo", "покрыти", "ликвидн", "выручк", "рентабельн", "капитал", "левередж", "леверидж",
            "процентн", "fcf", "денежн", "маржин", "прибыл", "обязательств", "погашен", "рефинанс", "оферт")
BASE = "https://raexpert.ru"


@dataclass
class DebtLine:
    secid: str
    name: str
    outstanding_mln: Optional[float]     # объём в обращении, млн руб. (ISSUESIZEPLACED × текущий номинал)
    coupon: Optional[float]              # % годовых
    maturity: Optional[date]
    offer: Optional[date]
    amortization: bool
    price: Optional[float]
    ytw: Optional[float]
    in_screen: bool


@dataclass
class DebtMap:
    issuer: str
    lines: list[DebtLine] = field(default_factory=list)

    @property
    def total_mln(self) -> float:
        return sum(x.outstanding_mln or 0.0 for x in self.lines)

    def schedule(self, settle: date, by_offer: bool = True) -> dict[int, float]:
        """Объём к погашению по годам (млн руб.); by_offer — оферту считаем датой возможного погашения."""
        out: dict[int, float] = {}
        for x in self.lines:
            d = x.offer if (by_offer and x.offer and x.offer > settle) else x.maturity
            if d is None:
                continue
            out[d.year] = out.get(d.year, 0.0) + (x.outstanding_mln or 0.0)
        return dict(sorted(out.items()))

    def describe(self, settle: date) -> str:
        sch = self.schedule(settle)
        parts = [f"{y}: {v:,.0f}".replace(",", " ") for y, v in sch.items()]
        return f"публичный долг {self.total_mln:,.0f} млн руб. в {len(self.lines)} выпусках; к погашению/оферте по годам: " + ", ".join(parts)


def debt_map(issuer: str, bonds: list[tuple[Bond, Quote]], metrics: dict[str, float], in_screen: set[str]) -> DebtMap:
    """bonds — (бумага, котировка) выпуски эмитента; metrics — secid -> YTW по нашему расчёту (если есть)."""
    dm = DebtMap(issuer)
    for b, q in sorted(bonds, key=lambda bq: bq[0].maturity or date.max):
        size = (b.issue_size * b.face_value / 1e6) if b.issue_size and b.face_value else None
        dm.lines.append(DebtLine(b.secid, b.name, size, b.coupon_percent, b.maturity, b.offer_date if b.has_offer else None,
                                 b.has_amortization, q.price, metrics.get(b.secid), b.secid in in_screen))
    return dm


def _text(html_text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", html_text))).strip()


def _site_base(url: str) -> str:
    """https://host по ссылке на страницу компании (относительные ссылки на релизы достраиваются к тому же сайту)."""
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else BASE


def release_links(company_html: str, base: str = BASE) -> list[str]:
    """Ссылки на пресс-релизы со страницы компании, в порядке появления (у Эксперт РА и НКР — свежие первыми), без дублей.
    Сама страница списка релизов (…/press-releases/ без слага) не считается."""
    out: list[str] = []
    for href in _RELEASE_HREF_RE.findall(company_html):
        href = html.unescape(href)
        if re.search(r"/press-releases/?$", href):
            continue
        url = href if href.startswith("http") else base + href
        if url not in out:
            out.append(url)
    return out


@dataclass
class ReleaseDigest:
    url: str
    title: str
    date: Optional[date]
    sentences: list[str]

    def describe(self, limit: int = 14) -> str:
        head = f"{self.title} ({self.date})" if self.date else self.title
        body = "\n".join(f"  • {s}" for s in self.sentences[:limit])
        return f"{head}\n  {self.url}\n{body}" if body else f"{head}\n  {self.url}\n  (предложений с финансовыми метриками не найдено)"


_NKR_SLUG_DATE_RE = re.compile(r"/ratings/press-releases/[^/]*-(\d{2})(\d{2})(\d{2})/?$")


def _nkr_slug_date(url: str) -> Optional[date]:
    """У НКР дата релиза зашита в адрес: …/Polyplast-RA-101125/ = 10.11.2025 (первая дата в тексте — не дата релиза:
    там отчётная дата или погашение выпуска)."""
    m = _NKR_SLUG_DATE_RE.search(url or "")
    if not m:
        return None
    try:
        return date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def digest_release(url: str, page_html: str, max_sentences: int = 40) -> ReleaseDigest:
    """Заголовок, дата и предложения с финансовыми метриками из текста релиза."""
    m = _TITLE_RE.search(page_html)
    title = _text(m.group(1) or m.group(2)) if m else url
    text = _text(page_html)
    d = _nkr_slug_date(url)
    dm = None if d else _DATE_RE.search(text)
    if dm:
        try:
            d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            d = None
    sentences = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z«])", text):
        low = sent.lower()
        if 40 <= len(sent) <= 600 and any(k in low for k in KEYWORDS) and re.search(r"\d", sent):
            if sent not in sentences:
                sentences.append(sent)
        if len(sentences) >= max_sentences:
            break
    return ReleaseDigest(url, title, d, sentences)


def fetch_releases(company_url: str, fetch, limit: int = 2) -> tuple[list[ReleaseDigest], str]:
    """(дайджесты, диагностика). fetch(url) -> (status, content_type, text)."""
    try:
        status, _, page = fetch(company_url)
    except Exception as e:  # noqa: BLE001
        return [], f"страница компании недоступна: {e}"
    if status != 200 or not page:
        return [], f"страница компании: HTTP {status}"
    links = release_links(page, base=_site_base(company_url))
    # релизы по выпускам («…-bonds-RA-…», «…-RA-bond-…») чисел по эмитенту не содержат — сначала релизы по компании
    links.sort(key=lambda u: "bond" in u.rsplit("/", 2)[-2].lower() if u.rstrip("/").count("/") >= 3 else False)
    if not links:
        # у АКРА карточек компаний нет — адрес из книги рейтингов ведёт прямо на пресс-релиз, читаем его самого
        d = digest_release(company_url, page)
        if d.sentences:
            return [d], "страница — сам релиз (АКРА), прочитан"
        return [], f"на странице компании не найдено ссылок на релизы ({len(page)} байт; возможно, список подгружается скриптом)"
    out: list[ReleaseDigest] = []
    for url in links[:limit]:
        try:
            st, _, body = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("релиз %s: %s", url, e)
            continue
        if st == 200 and body:
            out.append(digest_release(url, body))
    return out, f"релизов на странице {len(links)}, прочитано {len(out)}"


# ---- ИНН эмитента с карточки агентства -----------------------------------------------------------------------------
_INN_RE = re.compile(r"ИНН\D{0,20}(\d{10})\b")
_OGRN_RE = re.compile(r"ОГРН\D{0,20}(\d{13})\b")


def inn_from_page(page_html: str) -> tuple[Optional[str], Optional[str]]:
    """(ИНН, ОГРН) с карточки компании у агентства (Эксперт РА, НКР). У ВДО-эмитентов много тёзок (ООО «ВУШ» в Воронеже,
    НП «ПСБ»), поэтому запрос в ГИР БО по имени ненадёжен; ИНН с карточки делает его точным."""
    text = _text(page_html)
    inn = _INN_RE.search(text)
    ogrn = _OGRN_RE.search(text)
    return (inn.group(1) if inn else None), (ogrn.group(1) if ogrn else None)


def inn_from_agency(candidates, fetch, extra_urls: Optional[list[str]] = None) -> tuple[Optional[str], str]:
    """ИНН по карточкам агентств из записей рейтингов (Rating.url) и по дополнительным адресам (релиз из книги метрик —
    Эксперт РА печатает ИНН в шапке релиза): (ИНН, адрес, где нашли) или (None, диагностика)."""
    seen: set[str] = set()
    urls = [getattr(c, "url", "") for c in candidates] + list(extra_urls or [])
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            st, _, page = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("карточка %s: %s", url, e)
            continue
        if st != 200 or not page:
            continue
        inn, _ = inn_from_page(page)
        if inn:
            return inn, url
    return None, ("карточек агентств нет" if not seen else f"ИНН не найден на {len(seen)} карточках")

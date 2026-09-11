"""Новостной фон по эмитенту: RSS-поиск по названию + общие ленты, словарная оценка тональности риска.

Зачем: рейтинг обновляется раз в год, отчётность — раз в год с лагом, существенные факты приходят, когда
дефолт уже случился. Новости (иски, обыски, отзыв лицензии, срыв контракта, смена собственника) опережают
всё это на недели. Модуль не пытается «понять» новость — он ловит маркеры риска по словарю и считает
взвешенный по времени балл: отрицательный балл → штраф в композите стратегии, сильный негатив → стоп-фактор.

Источники (все — RSS, доступны и из-за рубежа):
  * Google News:  https://news.google.com/rss/search?q=<эмитент>&hl=ru&gl=RU&ceid=RU:ru
  * Bing News:    https://www.bing.com/news/search?q=<эмитент>&format=rss&setlang=ru
  * общие ленты Интерфакс / РБК / Коммерсант / Финам — фильтруются по названию эмитента.

Книга новостей — data/news.csv: date,query,inn,source,title,url,score,tags.
"""
from __future__ import annotations

import csv
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional

from .ratings import issuer_match, issuer_tokens
from .ratings_web import fetch

log = logging.getLogger(__name__)

SEARCH_FEEDS = {
    "google": "https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru",
    "bing": "https://www.bing.com/news/search?q={q}&format=rss&setlang=ru&cc=RU",
}
GENERAL_FEEDS = {
    "interfax": "https://www.interfax.ru/rss.asp",
    "rbc": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "kommersant": "https://www.kommersant.ru/RSS/news.xml",
    "finam": "https://www.finam.ru/analysis/conews/rsspoint/",
}

# Словарь маркеров: (тег, вес, регулярное выражение). Вес < 0 — риск, > 0 — поддержка.
LEXICON: list[tuple[str, float, re.Pattern]] = [
    ("default", -4.0, re.compile(r"дефолт|не (?:выплат|исполн)|просроч|невыплат", re.I)),
    ("bankruptcy", -4.0, re.compile(r"банкрот|несостоятельн|конкурсн\w+ (?:производств|управля)|ликвидац", re.I)),
    ("criminal", -3.0, re.compile(r"обыск|задержан|арестован|уголовн|мошеннич|под стражу|следственн\w+ комитет|\bСКР?\b(?=.*(?:возбуд|дело|обыск|задерж))")),
    ("license", -3.0, re.compile(r"отзыв\w* лиценз|лишил\w* лиценз|аннулир\w* лиценз|исключ\w* из реестра", re.I)),
    ("restructuring", -3.0, re.compile(r"реструктуризац|ПВО\b|собрани\w+ владельцев облигаций|отсрочк\w+ (?:выплат|платеж)", re.I)),
    ("sanctions", -2.0, re.compile(r"санкци|SDN|блокирующ", re.I)),
    ("rating_down", -2.0, re.compile(r"(?:пониз|сниз|ухудш)\w* (?:кредитн\w+ )?рейтинг|рейтинг\w* (?:пониж|сниж)|негативн\w+ прогноз|под наблюдени", re.I)),
    ("lawsuit", -1.5, re.compile(r"\bиск(?:а|у|ом|е|и|ов|ам|ами|ах)?\b|подал\w* в суд|арбитраж|судебн\w+ (?:иск|разбират|спор|процесс)|взыска|"
                                   r"претензи\w+ (?:ФНС|налогов)|ФНС (?:втянул|предъяв|подал|взыск|доначисл)|налогов\w+ (?:претенз|задолж|проверк|доначисл)", re.I)),
    ("loss", -1.5, re.compile(r"убыт(?:ок|ки|очн)|падени\w+ (?:выручк|прибыл)|сокращ\w+ (?:выручк|прибыл)|отрицательн\w+ (?:капитал|денежн)", re.I)),
    ("management", -1.0, re.compile(r"отставк|уволен|сменил\w* (?:гендиректор|руководител|собственник)|смена (?:собственник|владельц|руководств)", re.I)),
    ("debt", -1.0, re.compile(r"долгов\w+ нагрузк|задолженност|кредитор\w* требу|не смог\w* (?:рефинанс|погасить)", re.I)),
    ("rating_up", 2.0, re.compile(r"(?:повыс|повыш|улучш)\w* (?:кредитн\w+ )?рейтинг|рейтинг\w* (?:повыш|подтвержд)|позитивн\w+ прогноз|стабильн\w+ прогноз", re.I)),
    ("paid", 1.5, re.compile(r"(?:выплат|погас)\w*(?:\s+\S+){0,2}\s+(?:купон|облигац|долг|выпуск)|исполнил\w* (?:обязательств|оферт)|досрочно погас", re.I)),
    ("growth", 1.0, re.compile(r"рост\w* (?:выручк|прибыл|портфел)|увелич\w* (?:выручк|прибыл)|рекордн\w+ (?:выручк|прибыл)|чист\w+ прибыл\w+ (?:выросл|увелич)", re.I)),
    ("deal", 1.0, re.compile(r"контракт|соглашени|привлек\w* (?:кредит|финансиров|инвестиц)|IPO|SPO|дивиденд", re.I)),
]
# Блоги/форумы/соцсети: мнения, а не факты — в балл не входят (сохраняются с тегом blog для просмотра)
BLOG_RE = re.compile(r"smart-?lab|смарт-?лаб|пульс|дзен|dzen|vk\.com|вконтакте|telegram|t\.me|пост инвестора|блог|forum|форум|pikabu|пикабу|"
                     r"投资|investing\.com|tinkoff\.ru/invest|бкс экспресс", re.I)
STOP_TAGS = {"default", "bankruptcy", "license"}   # одна такая новость из СМИ — стоп-фактор
NOISE_RE = re.compile(r"разме(?:щ|ст)\w+ (?:облигаци|выпуск)|купон\w* ставк|ставк\w* купон|книг\w* заявок|сбор заявок|ориентир", re.I)  # рутина первичного рынка — не сигнал


@dataclass
class NewsItem:
    date: date
    query: str            # по какому названию искали (ключ сопоставления)
    source: str
    title: str
    url: str = ""
    inn: str = ""
    score: float = 0.0
    tags: str = ""


@dataclass
class NewsScore:
    n: int = 0
    negative: int = 0
    positive: int = 0
    score: float = 0.0            # сумма весов с затуханием по времени
    worst: Optional[NewsItem] = None
    items: list[NewsItem] = field(default_factory=list)
    stop: Optional[NewsItem] = None   # новость из СМИ с тегом дефолт/банкротство/отзыв лицензии за окно

    def describe(self) -> str:
        if not self.n:
            return "новостей нет"
        w = f"; худшая: {self.worst.date} «{self.worst.title[:60]}»" if self.worst and self.worst.score < 0 else ""
        return f"новостей {self.n} (негатив {self.negative}, позитив {self.positive}), балл {self.score:+.1f}{w}"


def score_title(title: str, source: str = "") -> tuple[float, list[str]]:
    t = html.unescape(title or "")
    score, tags = 0.0, []
    for tag, w, rx in LEXICON:
        if rx.search(t):
            score += w
            tags.append(tag)
    if not tags and NOISE_RE.search(t):
        tags.append("routine")
    if BLOG_RE.search(f"{source} {t}") or "?" in t:
        # мнения и вопросы («сможет ли расплатиться?») не считаем фактами
        tags.append("blog")
        score = 0.0
    return score, tags


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def parse_rss(text: str, source: str) -> list[tuple[date, str, str, str]]:
    """-> [(дата, заголовок, ссылка, издание)]; терпим к невалидному XML (fallback на regex)."""
    out = []
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
            src = (it.findtext("source") or source).strip()
            d = _parse_date(pub)
            if title and d:
                out.append((d, html.unescape(title), link, src))
        if out:
            return out
    except ET.ParseError:
        pass
    for m in re.finditer(r"<item>(.*?)</item>", text, re.S | re.I):
        block = m.group(1)
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        d = _parse_date(pub.group(1) if pub else "")
        if title and d:
            out.append((d, html.unescape(title.group(1).strip()), link.group(1).strip() if link else "", source))
    return out


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _clean_query(name: str) -> str:
    """Название эмитента для поисковой строки: существенные слова без ОПФ и номеров выпусков."""
    toks = issuer_tokens(name)
    return " ".join(toks[:3]) if toks else re.sub(r"\s+\S*\d\S*$", "", name).strip()


def search_news(name: str, sources: Iterable[str] = ("google", "bing"), days: int = 120, inn: str = "") -> list[NewsItem]:
    from urllib.parse import quote
    q = _clean_query(name)
    if not q:
        return []
    since = date.today() - timedelta(days=days)
    out: list[NewsItem] = []
    for src in sources:
        tpl = SEARCH_FEEDS.get(src)
        if not tpl:
            continue
        url = tpl.format(q=quote(f'"{q}"'))
        try:
            code, _, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s", url, e)
            continue
        if code != 200:
            log.warning("%s: HTTP %d", url, code)
            continue
        for d, title, link, publisher in parse_rss(text, src):
            if d < since:
                continue
            # у Google в заголовке хвост « - Издание»; отрезаем
            title = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", title)
            source = f"{src}:{publisher}"[:60]
            sc, tags = score_title(title, source)
            out.append(NewsItem(d, name, source, title, link, inn, sc, ",".join(tags)))
    return out


def scan_general_feeds(names: list[str], feeds: Optional[dict[str, str]] = None, days: int = 30) -> list[NewsItem]:
    """Общие ленты: оставляем новости, где встречается название одного из эмитентов."""
    since = date.today() - timedelta(days=days)
    out: list[NewsItem] = []
    for src, url in (feeds or GENERAL_FEEDS).items():
        try:
            code, _, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s", url, e)
            continue
        if code != 200:
            continue
        for d, title, link, _pub in parse_rss(text, src):
            if d < since:
                continue
            for name in names:
                if issuer_match(name, title):
                    sc, tags = score_title(title, src)
                    out.append(NewsItem(d, name, src, title, link, "", sc, ",".join(tags)))
                    break
    return out


# ---------------------------------------------------------------------------
# Книга новостей
# ---------------------------------------------------------------------------

class NewsBook:
    def __init__(self, items: Iterable[NewsItem] = ()):
        self.items: list[NewsItem] = []
        self._keys: set[tuple] = set()
        for it in items:
            self.add(it)

    def __len__(self) -> int:
        return len(self.items)

    def add(self, it: NewsItem) -> bool:
        key = (it.date, it.title.lower()[:80])
        if key in self._keys:
            return False
        self._keys.add(key)
        self.items.append(it)
        return True

    def for_issuer(self, name: str = "", inn: str = "") -> list[NewsItem]:
        out = []
        for it in self.items:
            if inn and it.inn and it.inn == inn:
                out.append(it)
            elif name and (it.query == name or issuer_match(it.query, name) or issuer_match(name, it.query)):
                out.append(it)
        return out

    def issuer_score(self, asof: date, name: str = "", inn: str = "", days: int = 90, half_life: float = 30.0) -> NewsScore:
        """Балл = Σ score·0.5^(возраст/half_life) по новостям за окно days."""
        since = asof - timedelta(days=days)
        ns = NewsScore()
        for it in self.for_issuer(name, inn):
            if not (since <= it.date <= asof):
                continue
            ns.n += 1
            ns.items.append(it)
            decay = 0.5 ** ((asof - it.date).days / half_life)
            ns.score += it.score * decay
            if it.score < 0:
                ns.negative += 1
                if ns.worst is None or it.score < ns.worst.score:
                    ns.worst = it
                if STOP_TAGS & set(it.tags.split(",")) and "blog" not in it.tags and (ns.stop is None or it.date > ns.stop.date):
                    ns.stop = it
            elif it.score > 0:
                ns.positive += 1
        ns.items.sort(key=lambda x: x.date, reverse=True)
        return ns

    @classmethod
    def from_csv(cls, path: str) -> "NewsBook":
        book = cls()
        if not path or not os.path.exists(path):
            return book
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    d = date.fromisoformat((r.get("date") or "")[:10])
                    sc = float(r.get("score") or 0)
                except ValueError:
                    continue
                book.add(NewsItem(d, (r.get("query") or "").strip(), (r.get("source") or "").strip(), (r.get("title") or "").strip(),
                                  (r.get("url") or "").strip(), (r.get("inn") or "").strip(), sc, (r.get("tags") or "").strip()))
        log.info("новости: %d записей из %s", len(book), path)
        return book

    def to_csv(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "query", "inn", "source", "title", "url", "score", "tags"])
            for it in sorted(self.items, key=lambda x: (x.date, x.query)):
                w.writerow([it.date.isoformat(), it.query, it.inn, it.source, it.title, it.url, f"{it.score:g}", it.tags])

    def prune(self, keep_days: int = 400) -> int:
        cutoff = date.today() - timedelta(days=keep_days)
        before = len(self.items)
        self.items = [it for it in self.items if it.date >= cutoff]
        self._keys = {(it.date, it.title.lower()[:80]) for it in self.items}
        return before - len(self.items)


def discover(query: str = "Балтийский лизинг") -> None:
    """Проверка доступности источников и качества разбора: печатает по 5 заголовков с баллами."""
    from urllib.parse import quote
    q = _clean_query(query)
    urls = {k: v.format(q=quote(f'"{q}"')) for k, v in SEARCH_FEEDS.items()}
    urls.update(GENERAL_FEEDS)
    for name, url in urls.items():
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== {name} {url}\n   ERROR {e}")
            continue
        items = parse_rss(text, name) if code == 200 else []
        print(f"== {name} {url}\n   HTTP {code} {ctype} len={len(text)} items={len(items)}")
        if not items:
            print("   head: " + re.sub(r"\s+", " ", text[:300]))
        for d, title, link, pub in items[:5]:
            sc, tags = score_title(title, f"{name}:{pub}")
            print(f"   {d} [{sc:+.1f} {','.join(tags) or '-'}] {title[:100]}  ({pub[:25]})")

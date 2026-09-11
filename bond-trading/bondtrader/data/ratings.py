"""Кредитные рейтинги: единая шкала, книга рейтингов, сопоставление с бумагами MOEX.

Источники (по убыванию надёжности сопоставления):
  1. ISIN выпуска (рейтинг выпуска или явная привязка);
  2. EMITTER_ID MOEX (из /iss/securities/{secid}.json, блок description);
  3. псевдоним — префикс краткого названия бумаги на MOEX (например «БалтЛиз» для «БалтЛизП16»).

Формат CSV (UTF-8, разделитель запятая, заголовок обязателен):
  subject,agency,rating,date,kind,isin,emitter_id,alias
  Балтийский лизинг,АКРА,AA-(RU),2025-06-10,issuer,,,БалтЛиз
  ...
Поля isin / emitter_id / alias могут быть пустыми — используется то, что есть.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from ..models import Bond

log = logging.getLogger(__name__)

# Единая шкала: индекс = «грейд», меньше — лучше.
SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
         "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]
GRADE = {r: i for i, r in enumerate(SCALE)}
INVESTMENT_GRADE_MAX = GRADE["BBB-"]   # рейтинги до BBB- включительно — инвестиционные по национальной шкале

_STRIP_RE = re.compile(r"\((RU|RUS)\)|\.RU$|^RU|\s+|ПРОГНОЗ.*$", re.IGNORECASE)


def normalize_rating(raw: Optional[str]) -> Optional[str]:
    """'ruBBB+', 'A-(RU)', 'BB+ (RU)', 'AA-.ru' -> 'BBB+', 'A-', 'BB+', 'AA-'. Отозванные/NR -> None."""
    if not raw:
        return None
    s = raw.strip().upper()
    if s in ("NR", "N/R", "-", "—", "ОТОЗВАН", "WITHDRAWN", "НЕТ"):
        return None
    s = _STRIP_RE.sub("", s)
    s = s.replace("RU", "") if s.startswith("RU") else s
    s = s.strip()
    # CCC+/CCC- сводим к CCC; SD/RD (выборочный дефолт) -> D
    if s.startswith("CCC"):
        return "CCC"
    if s in ("SD", "RD"):
        return "D"
    return s if s in GRADE else None


def grade(rating: Optional[str]) -> Optional[int]:
    r = normalize_rating(rating)
    return GRADE.get(r) if r else None


def rating_at_least(rating: Optional[str], minimum: str) -> bool:
    g, m = grade(rating), grade(minimum)
    return g is not None and m is not None and g <= m


@dataclass
class Rating:
    subject: str                      # эмитент или выпуск
    agency: str                       # АКРА / Эксперт РА / НКР / НРА
    rating: str                       # нормализованный, по шкале SCALE
    date: Optional[date] = None
    kind: str = "issuer"              # issuer | issue
    isin: str = ""
    emitter_id: str = ""
    alias: str = ""                   # префикс SHORTNAME на MOEX

    def __post_init__(self):
        norm = normalize_rating(self.rating)
        if norm is None:
            raise ValueError(f"нераспознанный рейтинг: {self.rating!r}")
        self.rating = norm

    @property
    def grade(self) -> int:
        return GRADE[self.rating]


class RatingsBook:
    """Коллекция рейтингов с поиском по бумаге."""

    def __init__(self, ratings: Iterable[Rating] = (), conservative: bool = True):
        self.conservative = conservative   # при нескольких агентствах брать худший рейтинг
        self.by_isin: dict[str, list[Rating]] = {}
        self.by_emitter: dict[str, list[Rating]] = {}
        self.by_alias: list[tuple[str, Rating]] = []
        self.by_subject: dict[str, list[Rating]] = {}
        self.all: list[Rating] = []
        for r in ratings:
            self.add(r)

    def add(self, r: Rating) -> None:
        self.all.append(r)
        if r.isin:
            self.by_isin.setdefault(r.isin.upper(), []).append(r)
        if r.emitter_id:
            self.by_emitter.setdefault(str(r.emitter_id), []).append(r)
        if r.alias:
            self.by_alias.append((_norm_name(r.alias), r))
        if r.subject and (r.kind == "issuer" or not r.isin):
            self.by_subject.setdefault(r.subject, []).append(r)

    def __len__(self) -> int:
        return len(self.all)

    def candidates(self, bond: Bond, emitter_id: Optional[str] = None) -> list[Rating]:
        if bond.isin and bond.isin.upper() in self.by_isin:
            return self.by_isin[bond.isin.upper()]
        if emitter_id and str(emitter_id) in self.by_emitter:
            return self.by_emitter[str(emitter_id)]
        name = _norm_name(bond.name)
        hits = [r for alias, r in self.by_alias if alias and name.startswith(alias)]
        if hits:
            # самый длинный псевдоним — самое точное совпадение
            best_len = max(len(_norm_name(r.alias)) for r in hits)
            return [r for r in hits if len(_norm_name(r.alias)) == best_len]
        if bond.full_name:
            out: list[Rating] = []
            for subject, rs in self.by_subject.items():
                if issuer_match(subject, bond.full_name):
                    out.extend(rs)
            if out:
                return out
        return []

    def lookup(self, bond: Bond, emitter_id: Optional[str] = None) -> Optional[Rating]:
        cands = self.candidates(bond, emitter_id)
        if not cands:
            return None
        # свежий рейтинг от каждого агентства
        latest: dict[str, Rating] = {}
        for r in cands:
            cur = latest.get(r.agency)
            if cur is None or (r.date or date.min) > (cur.date or date.min):
                latest[r.agency] = r
        pool = list(latest.values())
        return max(pool, key=lambda r: r.grade) if self.conservative else min(pool, key=lambda r: r.grade)

    # ---- загрузка ----
    @classmethod
    def from_csv(cls, path: str, conservative: bool = True) -> "RatingsBook":
        book = cls(conservative=conservative)
        if not os.path.exists(path):
            log.warning("файл рейтингов %s не найден — скринер работает без рейтингов", path)
            return book
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                r = parse_rating_row(row)
                if r:
                    book.add(r)
        log.info("рейтинги: загружено %d записей из %s", len(book), path)
        return book

    def to_csv(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["subject", "agency", "rating", "date", "kind", "isin", "emitter_id", "alias"])
            for r in self.all:
                w.writerow([r.subject, r.agency, r.rating, r.date.isoformat() if r.date else "", r.kind, r.isin, r.emitter_id, r.alias])


def parse_rating_row(row: dict) -> Optional[Rating]:
    rating = normalize_rating(row.get("rating"))
    subject = (row.get("subject") or "").strip()
    if not rating or not subject:
        return None
    d = None
    if row.get("date"):
        try:
            d = date.fromisoformat(row["date"].strip()[:10])
        except ValueError:
            d = None
    return Rating(subject=subject, agency=(row.get("agency") or "?").strip(), rating=rating, date=d,
                  kind=(row.get("kind") or "issuer").strip(), isin=(row.get("isin") or "").strip().upper(),
                  emitter_id=str(row.get("emitter_id") or "").strip(), alias=(row.get("alias") or "").strip())


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower().replace("ё", "е"))


_LEGAL_RE = re.compile(r"\b(ооо|ао|пао|зао|оао|нао|ип|мкао|мфк|мкк|гк|ук|ик|лк|фк|нко|общество с ограниченной ответственностью|"
                       r"публичное акционерное общество|акционерное общество|закрытое акционерное общество|"
                       r"limited|llc|plc|ltd|jsc|pjsc|ojsc|компани\w*|корпорац\w*|групп\w*|холдинг\w*|финанс\w*|инвест\w*|капитал\w*)\b")
_STOP = {"бо", "бо-п", "серии", "серия", "выпуск", "выпуска", "облигаций", "облигации", "биржевых", "биржевые", "и"}


# Сокращения в кратких именах MOEX -> как эмитент называется у агентств
MOEX_ISSUER_ALIASES = {
    "гостранспортлизингкомп": "гтлк",
    "мобильные телесистемы": "мтс",
    "гмк нор.никель": "гмк норильский никель",
    "гмк норникель": "гмк норильский никель",
    "деп финансов янао": "ямало-ненецкий автономный округ",
    "минфин амурской обл.": "амурская область",
    "евразхолдинг финанс": "евраз",
}


def issuer_tokens(name: str) -> list[str]:
    """Существенные слова названия эмитента без организационно-правовой формы и кавычек."""
    s = (name or "").lower().replace("ё", "е")
    for k, v in MOEX_ISSUER_ALIASES.items():
        if k in s:
            s = s.replace(k, v)
    s = re.sub(r"[«»\"'().,;:/\\-]", " ", s)
    s = _LEGAL_RE.sub(" ", s)
    toks = []
    for t in re.split(r"\s+", s):
        # маркеры MOEX: «i» — сектор инноваций, «s» — устойчивое развитие (iКаршеринг, sГТЛК)
        if len(t) >= 2 and t[0] in ("i", "s") and re.match(r"[а-я]", t[1]):
            t = t[1:]
        if len(t) >= 3 and t not in _STOP and not re.search(r"[0-9]", t):
            toks.append(t)
    return toks


_GENERIC_RE = re.compile(r"^(государствен|коммерческ|российск|национальн|федеральн|объединенн|публичн|акционерн|"
                         r"микрофинансов|лизингов|страхов|инвестиционн|управляющ|специализирован|торгов|производствен|"
                         r"промышленн|научн|транспортн|строительн|девелоп)")


def issuer_match(subject: str, full_name: str) -> bool:
    """Существенные слова названия эмитента из рейтинга встречаются в полном имени выпуска MOEX.

    Сначала требуем совпадения всех слов; если не вышло — убираем общие прилагательные
    («государственная», «российская», …) и проверяем оставшиеся.
    """
    st = issuer_tokens(subject)
    ft = set(issuer_tokens(full_name))
    if not st or not ft:
        return False
    # допускаем усечения: «Балтийский лизинг» vs «Балт. лизинг», «Нор.никель» vs «Норильский никель»
    def hit(t: str) -> bool:
        for f in ft:
            if f[:6] == t[:6]:
                return True
            if len(f) <= 4 and t.startswith(f) or len(t) <= 4 and f.startswith(t):
                return True
        return False
    if all(hit(t) for t in st):
        return True
    core = [t for t in st if not _GENERIC_RE.match(t)]
    return bool(core) and len(core) < len(st) and all(hit(t) for t in core)

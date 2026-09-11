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

    PRIMARY = "Эксперт РА"                 # единый источник: реестр Эксперт РА (полное актуальное состояние, не пресс-релизы)
    FALLBACK = ("НКР", "АКРА")             # только если у Эксперт РА рейтинга нет; агентство видно в rating_str

    def __init__(self, ratings: Iterable[Rating] = (), conservative: bool = True, policy: str = "primary",
                 primary: str = "", fallback: Optional[list] = None):
        self.conservative = conservative   # policy=worst: при нескольких агентствах брать худший рейтинг
        self.policy = policy               # primary | worst
        self.primary = primary or self.PRIMARY
        self.fallback = list(fallback) if fallback else list(self.FALLBACK)
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
            # только субъекты, которые и есть этот эмитент (в обе стороны), и только лучшие по совпадению —
            # иначе к РусГидро подмешивался «СГ РУС», а к Совкомбанк Лизингу — Совкомбанк и ПР-Лизинг,
            # и правило «худший рейтинг из агентств» брало рейтинг чужой организации
            scored = [(issuer_same(subject, bond.full_name), subject) for subject in self.by_subject]
            best = max((sc for sc, _ in scored), default=0.0)
            if best > 0:
                out: list[Rating] = []
                for sc, subject in scored:
                    if sc == best:
                        out.extend(self.by_subject[subject])
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
        if self.policy == "primary":
            if self.primary in latest:
                return latest[self.primary]
            for ag in self.fallback:
                if ag in latest:
                    return latest[ag]
            return max(pool, key=lambda r: r.grade)
        return max(pool, key=lambda r: r.grade) if self.conservative else min(pool, key=lambda r: r.grade)

    def latest_by_agency(self, bond: Bond, emitter_id: Optional[str] = None) -> dict[str, Rating]:
        """Свежая запись каждого агентства по бумаге — для аудита расхождений."""
        latest: dict[str, Rating] = {}
        for r in self.candidates(bond, emitter_id):
            cur = latest.get(r.agency)
            if cur is None or (r.date or date.min) > (cur.date or date.min):
                latest[r.agency] = r
        return latest

    def drop_agency(self, agency: str) -> int:
        """Убрать все записи агентства (перед полной перезагрузкой его реестра)."""
        keep = [r for r in self.all if r.agency != agency]
        n = len(self.all) - len(keep)
        self.by_isin.clear(); self.by_emitter.clear(); self.by_alias.clear(); self.by_subject.clear(); self.all.clear()
        for r in keep:
            self.add(r)
        return n

    # ---- загрузка ----
    @classmethod
    def from_csv(cls, path: str, conservative: bool = True, policy: str = "primary", primary: str = "",
                 fallback: Optional[list] = None) -> "RatingsBook":
        book = cls(conservative=conservative, policy=policy, primary=primary, fallback=fallback)
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


_LEGAL_RE = re.compile(r"\b(ооо|ао|пао|зао|оао|нао|ип|мкао|мфк|мкк|пко|кпк|гк|ук|ик|лк|фк|нко|общество с ограниченной ответственностью|"
                       r"публичное акционерное общество|акционерное общество|закрытое акционерное общество|"
                       r"limited|llc|plc|ltd|jsc|pjsc|ojsc|компани\w*|корпорац\w*|групп\w*|холдинг\w*|финанс\w*|инвест\w*|капитал\w*)\b")
# та же ОПФ-чистка, но «финанс/инвест/капитал» остаются значимыми словами (для названий с МФК/МКК)
_LEGAL_RE_MFO = re.compile(_LEGAL_RE.pattern.replace("|финанс\\w*|инвест\\w*|капитал\\w*", ""))
_NAMED_NUMBER_RE = re.compile(r"^([а-яa-z]{2,}|[а-яa-z](?=[0-9]{3}))-?[0-9]+$")   # ТГК-14, А101; но не П16 / Т1 / 001Р
_SERIES_WORDS = {"бо", "пбо", "бп", "пб", "зо", "суб", "sb", "bo", "sub", "по"}          # БО-01, ПБО-08, ЗО28 — серии, не имена
_STOP = {"бо", "бо-п", "пбо", "боп", "по", "зо", "серии", "серия", "выпуск", "выпуска", "облигаций", "облигации", "биржевых", "биржевые", "и"}


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
    # серия, приклеенная к имени без пробела («Аэрофьюэлз002Р-06», «Аэрофьюэлз-002Р-04», «АЛИУМ01Р1») — отделяем,
    # иначе слово с цифрами выбрасывается целиком и у эмитента не остаётся ни одного слова для сопоставления
    s = re.sub(r"(?<=[а-яa-z]{3})(-?[0-9]{3,}|[0-9]{2,}[а-яa-z]|[0-9][а-яa-z][0-9])", r" \1", s)
    # номер в имени эмитента — часть имени: «ТГК-14» ≠ «ТГК-1», «А101»; дефис между буквой и цифрой сохраняем
    s = re.sub(r"(?<=[а-яa-z])-(?=[0-9])", "\x00", s)
    s = re.sub(r"[«»\"'().,;:/\\-]", " ", s).replace("\x00", "-")
    # «ЭН+ГИДРО» и «ЭН ПЛЮС ГИДРО» — одно и то же: знак «+» читаем как слово «плюс»
    s = re.sub(r"\+", " плюс ", s)
    # у МФО/МКК слова «финанс», «капитал», «инвест» — часть имени: МФК «ПСБ Финанс» ≠ ПАО «ПСБ»
    legal_re = _LEGAL_RE_MFO if re.search(r"\b(мфк|мкк)\b", s) else _LEGAL_RE
    s = legal_re.sub(" ", s)
    toks = []
    for t in re.split(r"\s+", s):
        # маркеры MOEX: «i» — сектор инноваций, «s» — устойчивое развитие (iКаршеринг, sГТЛК)
        if len(t) >= 2 and t[0] in ("i", "s") and re.match(r"[а-я]", t[1]):
            t = t[1:]
        if len(t) < 3 or t in _STOP:
            continue
        # слова с цифрами — серии выпусков (001Р-06, БО-П16, 1P5), кроме имён вида «ТГК-14» / «А101»: ≥2 букв + номер
        if re.search(r"[0-9]", t):
            m = _NAMED_NUMBER_RE.match(t)
            if not m or m.group(1) in _SERIES_WORDS:
                continue
        toks.append(t)
    return toks


_GENERIC_RE = re.compile(r"^(государствен|коммерческ|российск|национальн|федеральн|объединенн|публичн|акционерн|"
                         r"микрофинансов|лизингов|страхов|инвестиционн|управляющ|специализирован|торгов|производствен|"
                         r"промышленн|научн|транспортн|строительн|девелоп)")


def _tok_eq(a: str, b: str) -> bool:
    """Одно и то же слово с точностью до склонения/усечения: общий префикс не короче 6 букв и не короче длины
    короткого слова минус 3 («технология»/«технологии» — да, «совкомбанк»/«совкомфлот» — нет)."""
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 5:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(6, n - 3)


def issuer_same(subject: str, full_name: str) -> float:
    """Та же ли организация: субъект рейтинга и полное имя выпуска MOEX (без ОПФ и серии).

    В отличие от issuer_match (вхождение эмитента в текст новости) требует совпадения В ОБЕ СТОРОНЫ:
    все существенные слова субъекта есть в имени выпуска и наоборот. «ПАО Совкомбанк» ≠ «Совкомбанк Лизинг»,
    «ООО СГ РУС» ≠ «РусГидро», «МФК Вэббанкир» ≠ «ВЭБ.РФ», «ООО Технология» ≠ «Облачные технологии».
    Возвращает 0 (не та) или долю совпавших слов (1.0 — полное совпадение).
    """
    st, ft = issuer_tokens(subject), issuer_tokens(full_name)
    if not st or not ft:
        return 0.0
    ms = [t for t in st if any(_tok_eq(t, f) for f in ft)]
    mf = [f for f in ft if any(_tok_eq(t, f) for t in st)]
    if not ms or all(_GENERIC_RE.match(t) for t in ms):
        return 0.0
    # имя выпуска должно быть покрыто целиком: «Совкомбанк Лизинг» ≠ «Совкомбанк»
    if any(f not in mf and not _GENERIC_RE.match(f) for f in ft):
        return 0.0
    extra = [t for t in st if t not in ms and not _GENERIC_RE.match(t)]
    if extra:
        # субъект длиннее краткого имени MOEX («Арлифт» ↔ «Арлифт Интернешнл»): допускаем одно лишнее слово,
        # если совпавшее слово длинное и характерное; оценка ниже полной, точный субъект всегда выиграет
        if len(extra) > 1 or max(len(t) for t in ms) < 6:
            return 0.0
    return (len(ms) + len(mf)) / (len(st) + len(ft))


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

"""Финансовая отчётность эмитентов (РСБУ) и существенные факты.

Источники:
  * ГИР БО ФНС (bo.nalog.ru) — годовая бухгалтерская отчётность всех юрлиц. У сайта есть JSON-эндпоинты,
    которыми пользуется его интерфейс; они не задокументированы, поэтому сначала разведка (discover),
    затем парсеры под реальные ответы.
  * Центр раскрытия (e-disclosure.ru) — существенные факты (дефолты, техдефолты, оферты) и отчёты эмитента.

Коды строк РСБУ (форма 1 — баланс, форма 2 — отчёт о финансовых результатах):
  1150 ОС, 1200 оборотные активы, 1250 деньги, 1240 фин. вложения краткосрочные, 1300 капитал,
  1400 долгосрочные обязательства, 1410 долгосрочные займы, 1500 краткосрочные обязательства, 1510 краткосрочные займы,
  1600 баланс; 2110 выручка, 2200 прибыль от продаж, 2320 проценты к получению, 2330 проценты к уплате,
  2300 прибыль до налогообложения, 2400 чистая прибыль.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from .ratings_web import HEADERS, fetch

log = logging.getLogger(__name__)

GIRBO_BASE = "https://bo.nalog.ru"
GIRBO_CANDIDATES = {
    "search_by_name": GIRBO_BASE + "/nbo/organizations/search?query={q}&page=0&size=10",
    "search_advanced": GIRBO_BASE + "/advanced-search/organizations/search?query={q}&page=0&size=10",
    "org_bfo": GIRBO_BASE + "/nbo/organizations/{id}/bfo/",
    "bfo_details": GIRBO_BASE + "/nbo/bfo/{id}/details",
}
EDISCLOSURE_CANDIDATES = {
    "search": "https://www.e-disclosure.ru/poisk-po-kompaniyam",
    "api_search": "https://www.e-disclosure.ru/api/search/companies?query={q}",
    "company": "https://www.e-disclosure.ru/portal/company.aspx?id={id}",
    "events": "https://www.e-disclosure.ru/portal/event.aspx?EventId={id}",
}


def fetch_json(url: str, timeout: float = 25) -> tuple[int, str, str]:
    """GET с Accept: application/json (SPA-бэкенды отдают JSON только по этому заголовку)."""
    from .tls import ru_ca_bundle
    h = dict(HEADERS)
    h["Accept"] = "application/json, text/plain, */*"
    h["X-Requested-With"] = "XMLHttpRequest"
    r = requests.get(url, headers=h, timeout=timeout, verify=ru_ca_bundle() or True)
    return r.status_code, r.headers.get("Content-Type", ""), r.text


def discover_bundle(base: str = GIRBO_BASE, index_path: str = "/") -> list[str]:
    """Скачивает JS-бандл SPA и вытаскивает из него пути внутреннего API."""
    code, _, html_text = fetch(base + index_path)
    scripts = re.findall(r'src="(/static/js/[^"]+\.js)"', html_text)
    paths: set[str] = set()
    for sc in scripts[:3]:
        try:
            _, _, js = fetch(base + sc)
        except Exception as e:  # noqa: BLE001
            print(f"   bundle {sc}: ERROR {e}")
            continue
        for m in re.findall(r"""["'`](/(?:nbo|api|advanced-search|bfo|organizations|search)[^"'`\s]{2,120})["'`]""", js):
            paths.add(m)
        for m in re.findall(r"""["'`](https?://[^"'`\s]*nalog[^"'`\s]{0,80})["'`]""", js):
            paths.add(m)
        print(f"   bundle {sc}: {len(js)} bytes, api-like paths: {sorted(paths)[:60]}")
    return sorted(paths)


# Агрегаторы данных ФНС, доступные из-за рубежа (ГИР БО и e-disclosure геоблокированы/антибот)
AGGREGATOR_CANDIDATES = {
    "audit_it": "https://www.audit-it.ru/buh_otchet/{inn}",
    "list_org_search": "https://www.list-org.com/search?type=inn&val={inn}",
    "checko": "https://checko.ru/company/{inn}",
    "rusprofile_search": "https://www.rusprofile.ru/search?query={inn}",
    "smartlab_bonds": "https://smart-lab.ru/q/bonds/",
}

MOEX_DEFAULTS_CANDIDATES = [
    "https://www.moex.com/ru/listing/emitent-defaults.aspx",
    "https://www.moex.com/ru/listing/default.aspx",
    "https://www.moex.com/s2830",
    "https://iss.moex.com/iss/securitygroups/stock_bonds/collections.json",
]


def discover(query: str = "Балтийский лизинг") -> None:
    """Печатает структуру ответов ГИР БО и e-disclosure для одного эмитента."""
    from urllib.parse import quote
    q = quote(query)
    print("== girbo: пути API из JS-бандла")
    try:
        discover_bundle()
    except Exception as e:  # noqa: BLE001
        print(f"   ERROR {e}")
    for name, tpl in GIRBO_CANDIDATES.items():
        if "{id}" in tpl:
            continue
        url = tpl.format(q=q)
        try:
            code, ctype, text = fetch_json(url)
            print(f"== girbo:{name} (Accept: json) {url}\n   HTTP {code} {ctype} len={len(text)}\n   head: " + re.sub(r"\s+", " ", text[:1200]))
        except Exception as e:  # noqa: BLE001
            print(f"== girbo:{name} (Accept: json) ERROR {e}")
    inn = query if query.isdigit() else "7826705374"
    for name, tpl in AGGREGATOR_CANDIDATES.items():
        url = tpl.format(inn=inn)
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== agg:{name} {url}\n   ERROR {e}")
            continue
        title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        print(f"== agg:{name} {url}\n   HTTP {code} {ctype} len={len(text)} title={title.group(1).strip()[:100] if title else '-'}")
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body)
        for kw in ("Выручка", "1300", "Баланс", "2110", "Капитал"):
            i = body.find(kw)
            if i >= 0:
                print(f"   near '{kw}': " + body[max(0, i - 300): i + 900])
                break
        else:
            print("   text: " + body[:800])
        tables = re.findall(r"<table.*?</table>", text, re.S | re.I)
        print(f"   tables={len(tables)}")
        for t in tables[:2]:
            print("   table head: " + re.sub(r"\s+", " ", t)[:700])
        links = sorted(set(re.findall(r'href="([^"]*(?:otchet|buh|finan|report)[^"]*)"', text, re.I)))[:15]
        print(f"   links: {links}")
    for url in MOEX_DEFAULTS_CANDIDATES:
        try:
            code, ctype, text = fetch(url)
            title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
            print(f"== moex-defaults {url}\n   HTTP {code} {ctype} len={len(text)} title={title.group(1).strip()[:80] if title else '-'}")
            body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            i = body.lower().find("дефолт")
            print("   sample: " + re.sub(r"\s+", " ", body[max(0, i - 200): i + 800]))
        except Exception as e:  # noqa: BLE001
            print(f"== moex-defaults {url}\n   ERROR {e}")
    for name, tpl in GIRBO_CANDIDATES.items():
        if "{id}" in tpl:
            continue
        url = tpl.format(q=q)
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== girbo:{name} {url}\n   ERROR {e}")
            continue
        print(f"== girbo:{name} {url}\n   HTTP {code} {ctype} len={len(text)}")
        print("   head: " + re.sub(r"\s+", " ", text[:1500]))
        if "json" in ctype:
            try:
                data = json.loads(text)
                items = data.get("content") if isinstance(data, dict) else data
                if isinstance(items, list) and items:
                    first = items[0]
                    print(f"   first item keys: {list(first)[:30]}")
                    org_id = first.get("id")
                    if org_id:
                        for sub in ("org_bfo",):
                            u = GIRBO_CANDIDATES[sub].format(id=org_id)
                            code2, ctype2, text2 = fetch(u)
                            print(f"== girbo:{sub} {u}\n   HTTP {code2} {ctype2} len={len(text2)}")
                            print("   head: " + re.sub(r"\s+", " ", text2[:2500]))
                            try:
                                bfo = json.loads(text2)
                                if isinstance(bfo, list) and bfo:
                                    print(f"   bfo[0] keys: {list(bfo[0])[:40]}")
                                    bid = bfo[0].get("id")
                                    if bid:
                                        u3 = GIRBO_CANDIDATES["bfo_details"].format(id=bid)
                                        code3, ctype3, text3 = fetch(u3)
                                        print(f"== girbo:bfo_details {u3}\n   HTTP {code3} {ctype3} len={len(text3)}")
                                        print("   head: " + re.sub(r"\s+", " ", text3[:4000]))
                            except ValueError:
                                pass
            except ValueError:
                pass
    for name, tpl in EDISCLOSURE_CANDIDATES.items():
        if "{id}" in tpl:
            continue
        url = tpl.format(q=q)
        try:
            code, ctype, text = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"== edisclosure:{name} {url}\n   ERROR {e}")
            continue
        print(f"== edisclosure:{name} {url}\n   HTTP {code} {ctype} len={len(text)}")
        forms = re.findall(r"<form[^>]*>", text, re.I)[:5]
        print("   forms: " + " | ".join(re.sub(r"\s+", " ", f)[:200] for f in forms))
        apis = sorted(set(re.findall(r"""["'](/api/[^"' ]{3,120}|[^"' ]*search[^"' ]{0,80})["']""", text, re.I)))[:20]
        print(f"   api/search hints: {apis}")
        print("   head: " + re.sub(r"\s+", " ", re.sub(r"<script.*?</script>", " ", text, flags=re.S)[:1500]))


# ---------------------------------------------------------------------------
# Модель отчётности и коэффициенты (не зависят от источника)
# ---------------------------------------------------------------------------

@dataclass
class Statement:
    """Годовая отчётность по РСБУ, тыс. руб. Ключи — коды строк как строки ('1300', '2110', ...)."""
    inn: str
    year: int
    values: dict[str, float] = field(default_factory=dict)
    source: str = "girbo"

    def v(self, code: str, default: float = 0.0) -> float:
        x = self.values.get(code)
        return float(x) if x is not None else default


@dataclass
class CreditMetrics:
    inn: str
    year: int
    revenue: float
    ebit: float                     # прибыль от продаж (2200)
    net_income: float
    equity: float
    total_debt: float               # 1410 + 1510
    net_debt: float                 # долг − деньги − краткосрочные фин. вложения
    cash: float
    short_debt_share: float         # 1510 / долг
    interest_expense: float         # 2330
    interest_coverage: Optional[float]   # EBIT / проценты
    net_debt_to_ebit: Optional[float]
    liabilities_to_equity: Optional[float]
    current_ratio: Optional[float]
    cash_to_short_debt: Optional[float]
    flags: list[str] = field(default_factory=list)
    score: float = 0.0              # 0..100, выше — лучше


def compute_metrics(st: Statement, prev: Optional[Statement] = None) -> CreditMetrics:
    revenue, ebit, ni = st.v("2110"), st.v("2200"), st.v("2400")
    equity = st.v("1300")
    lt_debt, st_debt = st.v("1410"), st.v("1510")
    debt = lt_debt + st_debt
    cash = st.v("1250") + st.v("1240")
    net_debt = debt - cash
    interest = abs(st.v("2330"))
    liabilities = st.v("1400") + st.v("1500")
    cur_assets, cur_liab = st.v("1200"), st.v("1500")

    def ratio(a: float, b: float) -> Optional[float]:
        return a / b if b and b > 0 else None

    m = CreditMetrics(
        inn=st.inn, year=st.year, revenue=revenue, ebit=ebit, net_income=ni, equity=equity,
        total_debt=debt, net_debt=net_debt, cash=cash,
        short_debt_share=(st_debt / debt) if debt > 0 else 0.0,
        interest_expense=interest,
        interest_coverage=ratio(ebit, interest),
        net_debt_to_ebit=(net_debt / ebit) if ebit > 0 else None,
        liabilities_to_equity=ratio(liabilities, equity),
        current_ratio=ratio(cur_assets, cur_liab),
        cash_to_short_debt=ratio(cash, st_debt),
    )
    score = 100.0
    if equity <= 0:
        m.flags.append("отрицательный капитал"); score -= 45
    if ebit <= 0:
        m.flags.append("операционный убыток"); score -= 25
    if m.interest_coverage is not None and m.interest_coverage < 1.5:
        m.flags.append(f"покрытие процентов {m.interest_coverage:.1f}x"); score -= 20
    elif m.interest_coverage is not None and m.interest_coverage < 2.5:
        score -= 8
    if m.net_debt_to_ebit is not None and m.net_debt_to_ebit > 5:
        m.flags.append(f"чистый долг/EBIT {m.net_debt_to_ebit:.1f}x"); score -= 15
    elif m.net_debt_to_ebit is not None and m.net_debt_to_ebit > 3:
        score -= 7
    if m.liabilities_to_equity is not None and m.liabilities_to_equity > 5:
        m.flags.append(f"обязательства/капитал {m.liabilities_to_equity:.1f}x"); score -= 10
    if debt > 0 and m.short_debt_share > 0.6 and (m.cash_to_short_debt or 0) < 0.5:
        m.flags.append("риск рефинансирования: короткий долг без подушки"); score -= 15
    if m.current_ratio is not None and m.current_ratio < 1.0:
        m.flags.append(f"текущая ликвидность {m.current_ratio:.2f}"); score -= 8
    if prev is not None:
        if prev.v("2110") > 0 and revenue < 0.8 * prev.v("2110"):
            m.flags.append("выручка −20%+ за год"); score -= 10
        if prev.v("1300") > 0 and equity < 0.8 * prev.v("1300"):
            m.flags.append("капитал −20%+ за год"); score -= 8
    m.score = max(0.0, min(100.0, score))
    return m


def implied_grade(score: float) -> str:
    """Грубое соответствие балла рейтинговой ступени (для сравнения с агентствами)."""
    if score >= 90: return "A"
    if score >= 75: return "BBB"
    if score >= 60: return "BB"
    if score >= 40: return "B"
    return "CCC"

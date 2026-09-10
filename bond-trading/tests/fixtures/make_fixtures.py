"""Генератор синтетических фикстур в формате MOEX ISS (запуск: python tests/fixtures/make_fixtures.py).

Цены выводятся из целевых доходностей через bondtrader.analytics.bond_math, поэтому
ОФЗ лежат около кривой zcyc, а корпораты — на заданном G-спреде.
"""
import json
import math
import os
from datetime import date, timedelta

from bondtrader.analytics.bond_math import accrued_interest, build_cash_flows, duration_convexity, price_from_ytm
from bondtrader.analytics.curve import ZeroCurve
from bondtrader.models import Bond

HERE = os.path.dirname(os.path.abspath(__file__))
SETTLE = date(2025, 6, 2)
TD = SETTLE.isoformat()

SEC_COLS = ["SECID", "SHORTNAME", "SECNAME", "ISIN", "BOARDID", "PREVPRICE", "PREVWAPRICE", "PREVLEGALCLOSEPRICE", "MATDATE",
            "COUPONPERCENT", "COUPONVALUE", "COUPONPERIOD", "NEXTCOUPON", "ACCRUEDINT", "FACEVALUE", "INITIALFACEVALUE", "FACEUNIT",
            "CURRENCYID", "LOTSIZE", "LOTVALUE", "ISSUESIZE", "ISSUESIZEPLACED", "OFFERDATE", "BUYBACKPRICE", "BUYBACKDATE",
            "SECTYPE", "LISTLEVEL", "STATUS"]
MD_COLS = ["SECID", "BID", "OFFER", "LAST", "LCURRENTPRICE", "MARKETPRICE", "YIELD", "YIELDATPREVWAPRICE", "DURATION", "VALTODAY",
           "VOLTODAY", "NUMTRADES", "TRADINGSTATUS", "SYSTIME"]

periods = [0.25, 0.5, 0.75, 1, 2, 3, 5, 7, 10, 15, 20, 30]
vals = [19.8, 19.4, 19.0, 18.6, 17.3, 16.5, 15.7, 15.4, 15.2, 15.1, 15.0, 14.9]
CURVE = ZeroCurve(SETTLE, list(zip(periods, vals)))

# secid, name, board, maturity, coupon %, period, next coupon, spread b.p. (None -> фикс. цена), turnover, level, extra
SPEC = [
    ("SU26234RMFS3", "ОФЗ 26234", "TQOB", "2025-07-16", 4.5, 182, "2025-07-16", 0, 120e6, 1, {}),
    ("SU26229RMFS3", "ОФЗ 26229", "TQOB", "2025-11-12", 7.15, 182, "2025-11-12", 0, 90e6, 1, {}),
    ("SU26207RMFS9", "ОФЗ 26207", "TQOB", "2027-02-03", 8.15, 182, "2025-08-06", 0, 300e6, 1, {}),
    ("SU26236RMFS9", "ОФЗ 26236", "TQOB", "2028-05-17", 5.7, 182, "2025-11-19", 0, 250e6, 1, {}),
    ("SU26224RMFS4", "ОФЗ 26224", "TQOB", "2029-05-23", 6.9, 182, "2025-11-26", 0, 200e6, 1, {}),
    ("SU26244RMFS2", "ОФЗ 26244", "TQOB", "2034-03-15", 11.25, 182, "2025-09-24", 0, 600e6, 1, {}),
    ("SU26238RMFS4", "ОФЗ 26238", "TQOB", "2041-05-15", 7.1, 182, "2025-11-19", 0, 900e6, 1, {}),
    ("SU29014RMFS6", "ОФЗ 29014", "TQOB", "2026-03-25", 20.0, 91, "2025-06-25", None, 400e6, 1, {"price": 99.0}),
    ("SU52002RMFS1", "ОФЗ 52002", "TQOB", "2028-02-02", 2.5, 182, "2025-08-06", None, 50e6, 1, {"price": 80.0, "face": 1360}),
    ("RU000A106K43", "Сбер Sb42R", "TQCB", "2027-06-10", 9.5, 182, "2025-12-10", 120, 40e6, 1, {}),
    ("RU000A105GE2", "РЖД 1Р-27R", "TQCB", "2028-10-20", 8.9, 182, "2025-10-20", 150, 25e6, 1, {}),
    ("RU000A107RZ0", "Газпнф3P8R", "TQCB", "2026-08-15", 12.4, 182, "2025-08-15", 130, 60e6, 1, {}),
    ("RU000A104ZK2", "МТС 1P-21", "TQCB", "2029-03-01", 10.0, 182, "2025-09-01", 200, 15e6, 1, {"offer": "2026-09-01", "bb": 100}),
    ("RU000A103WV8", "ВИС Ф БП04", "TQCB", "2027-11-05", 13.0, 182, "2025-11-05", 550, 3e6, 2, {}),
    ("RU000A105XX1", "Сегежа3P4R", "TQCB", "2027-01-30", 16.0, 182, "2025-07-30", 2500, 8e6, 2, {}),
    ("RU000A1090Y7", "НовыйЭм 1P", "TQCB", "2026-06-01", 19.0, 91, "2025-06-15", 400, 0.5e6, 3, {}),
    ("RU000A102FR1", "Газпнф ПК", "TQCB", "2027-04-01", 21.0, 91, "2025-07-01", None, 20e6, 1, {"price": 100.5, "floater": True}),
    ("RU000A100XY9", "МинфинРСХБ$", "TQCB", "2027-04-01", 5.0, 182, "2025-10-01", None, 1e6, 1, {"price": 100.0, "cur": "USD"}),
    ("XS0000000001", "Нет цены", "TQCB", "2027-04-01", 5.0, 182, "2025-10-01", None, 0, 1, {"price": None}),
    ("RU000A100AMR", "АмортБонд", "TQCB", "2026-12-01", 12.0, 182, "2025-12-01", 300, 2e6, 2, {"face": 500, "ifv": 1000, "spread_pct": 3.0}),
]


def main():
    secs, mds = [], []
    for secid, name, board, mat, cpct, period, nxt, spread_bp, turnover, level, x in SPEC:
        face = x.get("face", 1000)
        ifv = x.get("ifv", 1000 if face <= 1000 else 1000)
        cval = round(face * cpct / 100 * period / 365, 2)
        b = Bond(secid=secid, name=name, board=board, face_value=face, initial_face_value=max(ifv, face),
                 coupon_percent=cpct, coupon_value=cval, coupon_period=period,
                 next_coupon=date.fromisoformat(nxt), maturity=date.fromisoformat(mat),
                 offer_date=date.fromisoformat(x["offer"]) if "offer" in x else None, buyback_price=x.get("bb"))
        acc = round(accrued_interest(b, SETTLE), 2)
        if spread_bp is None:
            price = x.get("price")
            ytm = None
            dur_days = int((b.maturity - SETTLE).days * 0.9)
        else:
            # подбираем цену так, чтобы YTM = кривая(дюрация) + спред (две итерации по дюрации)
            dur = (b.maturity - SETTLE).days / 365 * 0.85
            for _ in range(3):
                ytm = CURVE.yield_at(dur) + spread_bp / 100
                price = round(price_from_ytm(b, SETTLE, ytm), 2)
                flows = build_cash_flows(b, SETTLE)
                dur = duration_convexity(flows, SETTLE, ytm / 100)[0]
            dur_days = int(dur * 365)
            ytm = round(ytm, 2)
        secs.append([secid, name, name, "RU000" + secid[-6:], board, price, price, price, mat, cpct, cval, period, nxt, acc,
                     face, max(ifv, face), x.get("cur", "SUR"), x.get("cur", "SUR"), 1, face, 10_000_000, 10_000_000,
                     x.get("offer"), x.get("bb"), x.get("offer"), "3", level, "A"])
        if price is not None:
            sp = x.get("spread_pct", 0.1)
            mds.append([secid, round(price - sp / 2, 2), round(price + sp / 2, 2), price, price, price, ytm, ytm, dur_days,
                        turnover, turnover // 1000, 100, "T", TD + " 18:40:00"])
    with open(os.path.join(HERE, "bonds_board.json"), "w", encoding="utf-8") as f:
        json.dump({"securities": {"columns": SEC_COLS, "data": secs}, "marketdata": {"columns": MD_COLS, "data": mds}},
                  f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "zcyc.json"), "w") as f:
        json.dump({"yearyields": {"columns": ["tradedate", "tradetime", "period", "value"],
                                  "data": [[TD, "18:45:00", p, v] for p, v in zip(periods, vals)]}}, f, indent=1)

    def bz(coupons, amorts, offers):
        return {"coupons": {"columns": ["isin", "name", "issuevalue", "coupondate", "recorddate", "startdate", "initialfacevalue",
                                        "facevalue", "faceunit", "value", "valueprc", "value_rub"],
                            "data": [["", "", 1000, d, d, d, 1000, 1000, "SUR", v, None if v is None else round(v / 1000 * 365 / 182, 2), v]
                                     for d, v in coupons]},
                "amortizations": {"columns": ["isin", "name", "issuevalue", "amortdate", "facevalue", "initialfacevalue", "faceunit",
                                              "valueprc", "value", "value_rub", "data_source"],
                                  "data": [["", "", 1000, d, 1000, 1000, "SUR", v / 10, v, v, "maturity"] for d, v in amorts]},
                "offers": {"columns": ["isin", "name", "issuevalue", "offerdate", "offerdatestart", "offerdateend", "facevalue",
                                       "faceunit", "price", "value", "agent", "offertype"],
                           "data": [["", "", 1000, d, d, d, 1000, "SUR", p, 1000, "", t] for d, p, t in offers]}}

    with open(os.path.join(HERE, "bondization_mts.json"), "w", encoding="utf-8") as f:
        json.dump(bz([("2025-09-01", 49.86), ("2026-03-01", 49.86), ("2026-09-01", 49.86), ("2027-03-01", None), ("2027-09-01", None),
                      ("2028-03-01", None), ("2028-09-01", None), ("2029-03-01", None)],
                     [("2029-03-01", 1000.0)], [("2026-09-01", 100.0, "Оферта put")]), f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "bondization_amort.json"), "w", encoding="utf-8") as f:
        json.dump(bz([("2025-12-01", 29.9), ("2026-06-01", 29.9), ("2026-12-01", 14.95)],
                     [("2025-06-01", 500.0), ("2026-06-01", 250.0), ("2026-12-01", 250.0)], []), f, ensure_ascii=False, indent=1)

    xml = """<?xml version="1.0" encoding="utf-8"?><soap:Envelope><soap:Body><KeyRateXMLResponse><KeyRateXMLResult><KeyRate>
<KR><DT>2024-10-28T00:00:00+03:00</DT><Rate>21.00</Rate></KR>
<KR><DT>2024-09-16T00:00:00+03:00</DT><Rate>19.00</Rate></KR>
<KR><DT>2024-07-29T00:00:00+03:00</DT><Rate>18.00</Rate></KR>
<KR><DT>2025-06-09T00:00:00+03:00</DT><Rate>20.00</Rate></KR>
<KR><DT>2025-05-30T00:00:00+03:00</DT><Rate>21.00</Rate></KR>
</KeyRate></KeyRateXMLResult></KeyRateXMLResponse></soap:Body></soap:Envelope>"""
    with open(os.path.join(HERE, "cbr_keyrate.xml"), "w", encoding="utf-8") as f:
        f.write(xml)

    rows, v, d0 = [], 100.0, date(2024, 1, 9)
    for i in range(300):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        v *= 1 + 0.0003 + 0.002 * math.sin(i / 9.0)
        rows.append([d.isoformat(), round(v, 2)])
    with open(os.path.join(HERE, "index_history_page1.json"), "w") as f:
        json.dump({"history": {"columns": ["TRADEDATE", "CLOSE"], "data": rows[:100]}}, f)
    print("fixtures written:", len(secs), "bonds")


if __name__ == "__main__":
    main()

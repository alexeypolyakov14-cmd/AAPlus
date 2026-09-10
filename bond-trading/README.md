# bondtrader — торговая система для облигаций российского рынка

Самостоятельный Python-проект (не зависит от остального репозитория AA+). Покрывает полный цикл:

| Слой | Что делает | Модули |
|---|---|---|
| Данные | MOEX ISS (котировки TQOB/TQCB, графики купонов/амортизаций/оферт, кривая zcyc, история цен и индексов), ключевая ставка ЦБ (SOAP), SQLite-кэш | `data/moex.py`, `data/cbr.py`, `data/cache.py` |
| Аналитика | Денежные потоки, YTM / доходность к оферте, дюрация Маколея и модифицированная, выпуклость, DV01, НКД, G-спред к кривой ОФЗ, roll-down | `analytics/bond_math.py`, `analytics/curve.py` |
| Скринер | Фильтры (валюта, листинг, оборот, bid/ask, флоатеры, линкеры, амортизация, оферты, дистресс-спред) и композитный скор | `screener.py` |
| Стратегии | `ladder`, `spread`, `rate_cycle`, `carry` — единый интерфейс «контекст → целевые веса» | `strategies/` |
| Риск | Лимиты на бумагу / эмитента / долю корпоратов / дюрацию / долю дневного оборота, DV01, параметрический VaR | `risk.py` |
| Бэктест | Дневной событийный движок: купоны, амортизация, погашения, комиссия, проскальзывание, ребалансировка; метрики и сравнение с RGBITR/RUCBITR | `backtest/` |
| Исполнение | Бумажный брокер (состояние в JSON) и T-Invest API (песочница/бой) через REST, журнал ордеров, dry-run по умолчанию | `execution/` |
| CLI | `screen`, `curve`, `keyrate`, `bond`, `signals`, `trade`, `portfolio`, `backtest`, `sandbox-init` | `cli.py` |

> **Это не инвестиционная рекомендация.** Система — инструмент для собственных решений. Любую стратегию сначала прогоняйте в бэктесте и песочнице T-Invest. Боевой режим включается только явно (`--broker tinvest --live --confirm` + подтверждение `YES`).

## Установка

```bash
cd bond-trading
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # или: pip install -r requirements.txt
cp config.example.yaml config.yaml
pytest                            # 35 тестов, работают офлайн на фикстурах
```

Требуется Python 3.10+. Сетевые вызовы идут к `iss.moex.com`, `www.cbr.ru` и (для исполнения) `*.tinkoff.ru`.

## Быстрый старт

```bash
# Офлайн-демо на синтетических данных из tests/fixtures (без сети)
bondtrader --fixtures tests/fixtures screen --top 10
bondtrader --fixtures tests/fixtures signals -s carry -p top_n=6

# Реальные данные MOEX
bondtrader screen --top 30                       # скринер ОФЗ + корпоратов 1–2 уровня листинга
bondtrader screen --ofz-only --max-duration 3
bondtrader curve                                 # кривая бескупонной доходности ОФЗ и её наклон
bondtrader keyrate                               # ключевая ставка и фаза цикла ДКП
bondtrader bond SU26238RMFS4 --schedule          # карточка бумаги с расчётом и денежными потоками

# Сигналы стратегии относительно текущего портфеля (бумажный брокер по умолчанию)
bondtrader signals -s rate_cycle
bondtrader signals -s ladder -p edges=[1,2,3,5] -p per_bucket=2
bondtrader signals -s spread -p entry_z=1.5 -p top_n=6

# Бэктест на истории MOEX (вселенная — top-15 текущего скрина либо явный список)
bondtrader backtest -s carry --start 2023-01-10 --end 2024-12-30 --rebalance monthly --csv nav.csv
bondtrader backtest -s ladder --start 2022-01-10 --universe SU26207RMFS9,SU26224RMFS4,SU26238RMFS4 --trades

# Исполнение: сначала dry-run, затем --confirm
bondtrader trade -s carry                        # только показывает ордера
bondtrader trade -s carry --confirm              # исполняет на бумажном брокере (state/portfolio.json)
bondtrader portfolio                             # NAV, дюрация, DV01, VaR, экспозиция по эмитентам

# T-Invest: песочница
export TINVEST_TOKEN=t.xxxxx                     # токен с доступом к песочнице
# Хосты tinkoff.ru используют сертификаты УЦ Минцифры. Если система им не доверяет
# (ошибка CERTIFICATE_VERIFY_FAILED), соберите бандл и укажите его:
curl -sSo /tmp/rt.crt  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
curl -sSo /tmp/sub.crt https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt
cat "$(python -m certifi)" /tmp/rt.crt /tmp/sub.crt > ~/.bondtrader-ca.pem
export TINVEST_CA_BUNDLE=~/.bondtrader-ca.pem
bondtrader sandbox-init --amount 1000000         # открыть и пополнить счёт, id прописать в config.yaml
bondtrader trade -s carry --broker tinvest --confirm
bondtrader portfolio --broker tinvest
```

Параметры стратегий передаются через `-p key=value` (числа, `true/false`, списки `[1,2,3]`) и перекрывают `strategy.params` из `config.yaml`.

## Стратегии

| Имя | Идея | Ключевые параметры |
|---|---|---|
| `ladder` | Равные доли по корзинам дюрации; удерживает купленные бумаги, пока они проходят скрин и остаются в своей корзине | `edges`, `per_bucket`, `ofz_only`, `max_duration` |
| `spread` | Покупка корпоратов с аномально широким G-спредом (z-оценка по истории спреда или кросс-секционно внутри корзины дюрации × уровня листинга), продажа при сжатии; ОФЗ-якорь ликвидности | `entry_z`, `exit_z`, `top_n`, `min_history`, `ofz_anchor`, `max_spread_bp` |
| `rate_cycle` | Фаза цикла ЦБ (по последним решениям) и наклон кривой задают целевую дюрацию: смягчение → длинные ОФЗ, ужесточение → короткие + флоатеры, пауза при инверсии → средне-длинные | `short_dur`, `mid_dur`, `mid_long_dur`, `long_dur`, `band`, `top_n`, `floater_share` |
| `carry` | Ожидаемая доходность за горизонт = YTW + roll-down по кривой; топ-N с гарантированной долей ОФЗ | `top_n`, `horizon`, `max_duration`, `ofz_min_share`, `per_unit_duration` |

Все стратегии реализуют `Strategy.targets(ctx) -> {secid: вес}`; добавить свою — унаследовать `Strategy` в `strategies/` и зарегистрировать в `STRATEGIES`.

## Конвенции расчётов

* Эффективная годовая доходность, ACT/365, `t = (дата платежа − дата расчётов) / 365` — совпадает с методикой MOEX (в `screen` есть флаг «расхождение с YTM MOEX», если наш расчёт отличается больше чем на 1 п.п.).
* Если у бумаги есть оферта, рассчитывается доходность к оферте; фильтры и стратегии используют **YTW** (минимум из доходностей).
* Кривая: `zcyc` MOEX (блок `yearyields`), при недоступности — медианы YTM ОФЗ-ПД по корзинам дюрации. G-спред = YTM − кривая(дюрация Маколея), б.п.
* Плавающие купоны определяются по префиксу `SU29`, признакам в названии («ПК», «флоат», «КС+», «RUONIA») и по неизвестным будущим купонам в графике; для них YTM не считается.
* Ключевая ставка: режим `easing` / `tightening`, если последнее изменение было не позже 120 дней назад, иначе `hold`.

## Риск-лимиты (config.yaml → `risk`)

`max_weight_per_bond` (корпорат), `max_weight_per_bond_ofz`, `max_weight_per_issuer`, `max_corporate_share`, `min/max_portfolio_duration` (предупреждение), `max_g_spread_bp` (исключение дистресса), `max_turnover_share` (ордер не больше доли дневного оборота), `yield_vol_bp_daily` (σ доходностей для VaR). Жёсткие нарушения (нехватка денег, продажа без позиции) блокируют исполнение целиком; мягкие — режут веса и выводятся в отчёт.

## Бэктест: что учтено и что нет

Учтено: чистая цена + НКД, купоны и амортизации по `bondization`, погашение по номиналу, комиссия и проскальзывание, ребалансировка `daily|weekly|monthly|quarterly`, бенчмарк (индексы полной доходности MOEX), метрики: total return, CAGR, волатильность, Sharpe (безрисковая — средняя ключевая ставка периода), Sortino, max drawdown, Calmar, beta, tracking error, IR.

Не учтено: налоги, частичное исполнение и глубина стакана (исполнение по закрытию дня), **survivorship bias** при автоматической вселенной (берутся бумаги, торгующиеся сегодня). Для честного теста задавайте вселенную явно через `--universe` бумагами, существовавшими на начало периода. Исторические кривые zcyc подгружаются с `--curves` (медленнее); без флага кривая на каждую дату строится по ОФЗ из вселенной.

## Структура

```
bond-trading/
├── bondtrader/
│   ├── models.py            # Bond, Quote, BondMetrics, CashFlow
│   ├── config.py            # YAML + env
│   ├── market.py            # снимок рынка (MOEX/фикстуры)
│   ├── screener.py          # фильтры и скоринг
│   ├── portfolio.py         # позиции, сделки, купоны, NAV
│   ├── risk.py              # лимиты, DV01, VaR, ордера из целевых весов
│   ├── cli.py
│   ├── analytics/           # bond_math.py, curve.py
│   ├── data/                # moex.py, cbr.py, cache.py
│   ├── strategies/          # base.py, ladder.py, spread.py, rate_cycle.py, carry.py
│   ├── backtest/            # engine.py, data.py, metrics.py
│   └── execution/           # base.py, paper.py, tinvest.py, executor.py
├── tests/                   # pytest, офлайн на фикстурах формата MOEX ISS
│   └── fixtures/make_fixtures.py   # генератор фикстур (цены согласованы с кривой)
├── config.example.yaml
└── pyproject.toml
```

## Ограничения текущей версии

* Нет кредитных рейтингов (MOEX ISS их не отдаёт): качество эмитента аппроксимируется уровнем листинга и G-спредом. Подключение рейтингов АКРА/Эксперт РА — следующий шаг.
* Ключ эмитента для лимитов концентрации — первое слово названия бумаги; для точности стоит подтянуть `EMITTER_ID` из `/iss/securities/{secid}.json`.
* T-Invest: цена ордера — в процентах от номинала, количество — в лотах; сопоставление secid ↔ uid идёт через `BondBy` по ISIN/тикеру. Хосты песочницы и боя различаются, в песочнице по умолчанию.
* Сетевые вызовы к MOEX/ЦБ/T-Invest в тестах не выполняются — покрыты парсеры на сохранённых ответах и транспорт-заглушка.

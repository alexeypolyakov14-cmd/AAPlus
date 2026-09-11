# bondtrader — торговая система для облигаций российского рынка

Самостоятельный Python-проект (не зависит от остального репозитория AA+). Покрывает полный цикл:

| Слой | Что делает | Модули |
|---|---|---|
| Данные | MOEX ISS (котировки TQOB/TQCB, графики купонов/амортизаций/оферт, кривая zcyc, история цен и индексов), ключевая ставка ЦБ (SOAP), SQLite-кэш | `data/moex.py`, `data/cbr.py`, `data/cache.py` |
| Кредитный анализ | Рейтинги АКРА / Эксперт РА / НКР (парсеры сайтов, единая шкала), отчётность РСБУ из ГИР БО ФНС (балл 0..100 и флаги), существенные факты e-disclosure (дефолты, реструктуризации), новостной фон (RSS Google/Bing/Интерфакс/РБК, словарь маркеров риска), секторы, модель справедливого спреда | `data/ratings*.py`, `data/financials.py`, `data/girbo.py`, `data/disclosure.py`, `data/news.py`, `data/sectors.py`, `analytics/fair_spread.py` |
| Аналитика | Денежные потоки, YTM / доходность к оферте, дюрация Маколея и модифицированная, выпуклость, DV01, НКД, G-спред к кривой ОФЗ, roll-down | `analytics/bond_math.py`, `analytics/curve.py` |
| Скринер | Фильтры (валюта, листинг, оборот, bid/ask, флоатеры, линкеры, амортизация, оферты, дистресс-спред, минимальный рейтинг, стоп-факторы по отчётности / фактам / новостям) и композитный скор | `screener.py` |
| Стратегии | `ladder`, `spread`, `rate_cycle`, `carry`, `value_hy` — единый интерфейс «контекст → целевые веса» | `strategies/` |
| Риск | Лимиты на бумагу / эмитента / сектор / долю корпоратов / долю без рейтинга / дюрацию / долю дневного оборота, стоп по отчётности, DV01, параметрический VaR | `risk.py` |
| Бэктест | Дневной событийный движок: купоны, амортизация, погашения, комиссия, проскальзывание, ребалансировка; метрики и сравнение с RGBITR/RUCBITR | `backtest/` |
| Исполнение | Бумажный брокер (состояние в JSON) и T-Invest API (песочница/бой) через REST, журнал ордеров, dry-run по умолчанию | `execution/` |
| CLI | `screen`, `curve`, `keyrate`, `bond`, `signals`, `trade`, `portfolio`, `backtest`, `sandbox-init`, `ratings`, `financials`, `disclosure`, `news` | `cli.py` |

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

### Telegram: ежедневный отчёт и бот с кнопками

Секреты (GitHub → Settings → Secrets and variables → Actions или переменные окружения локально): `TELEGRAM_BOT_TOKEN`
(токен от @BotFather), `TELEGRAM_CHAT_ID` (ваш чат; узнать: напишите боту `/start`, затем `bondtrader notify --whoami`).

```bash
bondtrader peers --top 30                          # за что платят больше, чем за пиров (рейтинг ± сектор ± дюрация)
bondtrader peers "БалтЛиз"                          # группа пиров конкретной бумаги: кто похож и на сколько она дороже
bondtrader signals -s gspread -p rank=peers -p min_excess_bp=100 -p top_n=20   # отбор по превышению над пирами
bondtrader why "НЛМК"                              # все выпуски эмитента и почему (не) в скрине
bondtrader report --broker tinvest --telegram      # отчёт в чат: HTML-таблицы позиций/P&L, алерты, цель, новости + кнопки
bondtrader bot --menu                              # прислать справку с кнопками и ответить на накопившиеся запросы
bondtrader bot --broker tinvest --poll             # локально: long polling, ответы мгновенно
```

Кнопки «📊 Отчёт», «📋 Позиции», «📰 Новости», «🔎 Скрин ВДО», «🚨 Алерты» (и команды `/report`, `/positions`, `/news`,
`/screen`, `/alerts`, `/menu`). Бот отвечает только чату `TELEGRAM_CHAT_ID`. В GitHub Actions job `bot` запускается по
расписанию каждые 10 минут в 09:00–19:59 МСК по будням и обрабатывает накопившиеся нажатия (`bot` без `--poll`: ответ
приходит в течение 3–15 минут); смещение `state/telegram_offset.json` хранится в кэше Actions, чтобы не отвечать дважды.
Ежедневный отчёт (10:40 МСК) приходит с теми же кнопками.

Параметры стратегий передаются через `-p key=value` (числа, `true/false`, списки `[1,2,3]`) и перекрывают `strategy.params` из `config.yaml`.

## Стратегии

| Имя | Идея | Ключевые параметры |
|---|---|---|
| `ladder` | Равные доли по корзинам дюрации; удерживает купленные бумаги, пока они проходят скрин и остаются в своей корзине | `edges`, `per_bucket`, `ofz_only`, `max_duration` |
| `spread` | Покупка корпоратов с аномально широким G-спредом (z-оценка по истории спреда или кросс-секционно внутри корзины дюрации × уровня листинга), продажа при сжатии; ОФЗ-якорь ликвидности | `entry_z`, `exit_z`, `top_n`, `min_history`, `ofz_anchor`, `max_spread_bp` |
| `rate_cycle` | Фаза цикла ЦБ (по последним решениям) и наклон кривой задают целевую дюрацию: смягчение → длинные ОФЗ, ужесточение → короткие + флоатеры, пауза при инверсии → средне-длинные | `short_dur`, `mid_dur`, `mid_long_dur`, `long_dur`, `band`, `top_n`, `floater_share` |
| `carry` | Ожидаемая доходность за горизонт = YTW + roll-down по кривой; топ-N с гарантированной долей ОФЗ | `top_n`, `horizon`, `max_duration`, `ofz_min_share`, `per_unit_duration` |
| `value_hy` | Недооценённые ВДО: композит из остатка G-спреда к справедливому (регрессия по рейтингу, дюрации, обороту, листингу), разрыва «балл отчётности − балл рейтинга», доходности за вычетом ожидаемых потерь (YTW − PD·LGD − ликвидность) и новостного фона; стоп-факторы по отчётности и фактам; равные веса + ядро в ОФЗ | `top_n`, `w_spread`, `w_fin`, `w_ret`, `w_news`, `lgd`, `min_coverage`, `max_duration`, `ofz_min_share` |

Все стратегии реализуют `Strategy.targets(ctx) -> {secid: вес}`; добавить свою — унаследовать `Strategy` в `strategies/` и зарегистрировать в `STRATEGIES`.

## Конвенции расчётов

* Эффективная годовая доходность, ACT/365, `t = (дата платежа − дата расчётов) / 365` — совпадает с методикой MOEX (в `screen` есть флаг «расхождение с YTM MOEX», если наш расчёт отличается больше чем на 1 п.п.).
* Если у бумаги есть оферта, рассчитывается доходность к оферте; фильтры и стратегии используют **YTW** (минимум из доходностей).
* Кривая: `zcyc` MOEX (блок `yearyields`), при недоступности — медианы YTM ОФЗ-ПД по корзинам дюрации. G-спред = YTM − кривая(дюрация Маколея), б.п.
* Плавающие купоны определяются по префиксу `SU29`, признакам в названии («ПК», «флоат», «КС+», «RUONIA») и по неизвестным будущим купонам в графике; для них YTM не считается.
* Ключевая ставка: режим `easing` / `tightening`, если последнее изменение было не позже 120 дней назад, иначе `hold`.

## Кредитный анализ: откуда берутся данные

Четыре книги в `data/` (CSV, накапливаются, коммитятся в репозиторий или кэшируются в CI):

| Книга | Команда | Источник | Откуда работает |
|---|---|---|---|
| `ratings.csv` | `bondtrader ratings fetch` | сайты АКРА, Эксперт РА, НКР | отовсюду (CI обновляет каждый прогон) |
| `financials.csv` + `issuers.csv` | `bondtrader -c configs/hy.yaml financials fetch --from-screen` | ГИР БО ФНС (bo.nalog.ru, JSON-бэкенд SPA) | **только из РФ** (геоблок); кэш сырых ответов в `data/financials/` |
| `disclosure.csv` | `bondtrader -c configs/hy.yaml disclosure fetch --from-screen [--browser]` | e-disclosure.ru, существенные факты | **из РФ**; при антибот-проверке — `--browser` (Playwright: `pip install "bondtrader[browser]" && playwright install chromium`) |
| `news.csv` | `bondtrader -c configs/hy.yaml news fetch --from-screen [--general]` | Google News / Bing RSS по названию эмитента; общие ленты Интерфакс, РБК, Коммерсант, Финам | отовсюду |

Как это попадает в решения:
* скринер: `min_rating`, `require_rating`, `exclude_default_days` (свежий (тех)дефолт/реструктуризация — отсев), `min_fin_score`, `require_financials`, `news_stop_score` (свежая новость про дефолт/банкротство/обыски — отсев);
* риск: `fin_hard_stops` (отрицательный капитал, покрытие процентов < 1 — не покупаем), `max_sector_share`, `max_unrated_share`;
* стратегия `value_hy`: компоненты композита и их веса (см. `configs/hy.yaml`, веса подбираются по бэктесту);
* `financials show <ИНН|название>`, `financials coverage`, `disclosure list --kind default tech_default`, `news show --query <эмитент>` — для ручной проверки.

Балл отчётности (`data/financials.py`): 100 минус штрафы за отрицательный капитал (−45), операционный убыток (−25), покрытие процентов < 1.5 (−20), чистый долг/EBIT > 5 (−15), риск рефинансирования (короткий долг без подушки, −15), обязательства/капитал > 5 (−10), текущая ликвидность < 1 (−8), падение выручки/капитала > 20 % (−10/−8). Балл ≈ ступени рейтинга: ≥90 A, ≥75 BBB, ≥60 BB, ≥40 B, ниже — CCC.

Все парсеры внешних сайтов — регулярные выражения над HTML/JSON, они ломаются при смене вёрстки; для каждого источника есть команда `discover`, печатающая, что реально отдаёт сайт.

## Риск-лимиты (config.yaml → `risk`)

`max_weight_per_bond` (корпорат), `max_weight_per_bond_ofz`, `max_weight_per_issuer`, `max_sector_share`, `max_corporate_share`, `max_unrated_share`, `min_rating`, `fin_hard_stops`, `min/max_portfolio_duration` (предупреждение), `max_g_spread_bp` (исключение дистресса), `max_turnover_share` (ордер не больше доли дневного оборота), `yield_vol_bp_daily` (σ доходностей для VaR). Жёсткие нарушения (нехватка денег, продажа без позиции) блокируют исполнение целиком; мягкие — режут веса и выводятся в отчёт.

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
│   ├── monitor.py           # алерты по позициям (стоп-факторы, просадки, новости)
│   ├── report_tg.py         # секции отчёта для Telegram (HTML, <pre>-таблицы)
│   ├── notify.py            # Telegram Bot API: sendMessage, getUpdates, клавиатуры
│   ├── bot.py               # кнопки/команды бота: отчёт, позиции, новости, скрин, алерты
│   ├── analytics/           # bond_math.py, curve.py, fair_spread.py, peers.py (группа пиров)
│   ├── data/                # moex.py, cbr.py, cache.py, tls.py, ratings.py, ratings_web.py, financials.py, girbo.py, disclosure.py, news.py, sectors.py
│   ├── strategies/          # base.py, ladder.py, spread.py, rate_cycle.py, carry.py, value_hy.py, gspread.py
│   ├── backtest/            # engine.py, data.py, metrics.py
│   └── execution/           # base.py, paper.py, tinvest.py, executor.py
├── tests/                   # pytest, офлайн на фикстурах формата MOEX ISS
│   └── fixtures/make_fixtures.py   # генератор фикстур (цены согласованы с кривой)
├── configs/                 # moderate.yaml (ОФЗ+качественные корпораты), hy.yaml (ВДО, максимальный риск)
├── data/                    # книги: ratings.csv, financials.csv, issuers.csv, disclosure.csv, news.csv (кэш HTTP игнорируется git)
├── config.example.yaml
└── pyproject.toml
```

## Ограничения текущей версии

* Рейтинги сопоставляются с бумагами по ISIN выпуска, `EMITTER_ID` и совпадению названия эмитента (покрытие скрина ~90–96 %); остаток закрывается псевдонимами в `ratings.csv`/`issuers.csv`.
* ГИР БО и e-disclosure недоступны из-за рубежа (геоблок/антибот), поэтому книги отчётности и фактов заполняются локальным запуском из РФ и коммитятся; CI их только читает.
* Вероятности дефолта по ступеням рейтинга в `value_hy` — ориентировочные константы, а веса компонент — стартовые: их надо калибровать по бэктесту с историческими книгами.
* Ключ эмитента для лимитов концентрации — первое слово названия бумаги (ИНН используется, когда есть в `issuers.csv`).
* T-Invest: цена ордера — в процентах от номинала, количество — в лотах; сопоставление secid ↔ uid идёт через `BondBy` по ISIN/тикеру. Хосты песочницы и боя различаются, в песочнице по умолчанию.
* Сетевые вызовы к MOEX/ЦБ/T-Invest в тестах не выполняются — покрыты парсеры на сохранённых ответах и транспорт-заглушка.

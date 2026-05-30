# CLAUDE.md — погружение в проект за 5 минут

> Этот файл — единая точка входа для Claude (и нового разработчика). Прочитав его, ты знаешь **что делает проект, как он устроен, где что лежит, что уже сломалось и как чинить**. Дальше — в код или в `DEPLOY.md`.

---

## 1. Что это и зачем

**`ym_stocks_fetcher`** — skill для платформы **OpenClaw**, который каждое утро:

1. Тянет с **Яндекс.Маркет Partner API (FBY)** остатки, оборачиваемость, заказы и **заявки на поставку (товары в пути)**.
2. Пишет данные в **Google Sheets** (рабочая таблица руководителя — единый источник правды).
3. Считает **прогноз дней до OOS (out-of-stock)** с учётом остатков на МП, готовых в офисе и идущих поставок.
4. Шлёт сводку и алерты в **Telegram**.

Дополнительно реагирует на запросы в чате («покажи остатки», «продажи за неделю», «когда заказывать ЧХ5», «что в пути»), но это уже на стороне OpenClaw — здесь только данные.

**Кто пользователь:** руководитель / закупщик небольшого FBY-кабинета (~50 SKU). Google Sheets выбран осознанно — он уже умеет в ручные правки, формулы, графики и шаринг без отдельной админки.

---

## 2. Архитектура одним взглядом

```
                        ┌──────────────────────┐
                        │  Яндекс.Маркет API   │
                        │  (Partner, FBY)      │
                        └──────────┬───────────┘
                                   │
       ┌───────────────────┬───────┴───────┬────────────────────┐
       │                   │               │                    │
   05:30 cron          05:45 cron      06:00 cron           06:30 cron
fetch_ym_orders   fetch_ym_supplies  fetch_ym_stocks    forecast_stocks
       │                   │               │                    │
       │ заказы → продажи  │ заявки → в пути  остатки+turnover  читает всё
       │                   │ (текущий срез)│               считает days_to_oos
       ▼                   ▼               ▼                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          Google Sheets                                │
│  ym_orders_raw  sales_daily  supplies_planned  ym_stocks_raw         │
│  stock_marketplace  mp_warehouses  stock_office (опц.)               │
│  forecast  products                                                   │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                        Telegram (сводки + алерты)
```

**Поток данных (упрощённо):**

```
заказы (orders API) ──► ym_orders_raw ──► sales_daily ────────┐
поставки (supply-requests API) ──► supplies_planned ──────────┤
остатки (stocks API) ──► ym_stocks_raw ──► stock_marketplace ─┴─► forecast ──► Telegram
```

**15-30 минут между шагами** — это запас на медленный ответ API, не оптимизация.

---

## 3. Структура файлов

```
ym_stocks_fetcher/
├── CLAUDE.md              ← этот файл, точка входа
├── SKILL.md               ← манифест skill для OpenClaw (cron, триггеры)
├── DEPLOY.md              ← пошаговая установка на сервер (~1-2 часа)
├── CHANGES.md             ← changelog патчей (что чинилось и почему)
├── README.md              ← короткое описание для GitHub
│
├── fetch_ym_orders.py     ← модуль 2: заказы → ym_orders_raw + sales_daily
├── fetch_ym_supplies.py   ← модуль 3: заявки на поставку → supplies_planned
├── fetch_ym_stocks.py     ← модуль 1а: остатки → ym_stocks_raw + stock_marketplace
├── forecast_stocks.py     ← модуль 1б: прогноз days_to_oos → forecast + TG
├── sheets_helpers.py      ← ОБЩИЙ модуль: batch-операции, retry, санитизация
│
├── requirements.txt       ← requests, gspread, oauth2client, python-dotenv
├── .env.example           ← шаблон окружения (копировать в /opt/openclaw/secrets/.env)
└── .gitignore             ← .env, *.json (ключи), __pycache__, .venv
```

### Что в каждом файле — в одной строке

| Файл | Главное |
|---|---|
| `fetch_ym_orders.py` | `POST /v1/businesses/{id}/orders` → парсит позиции → upsert в `ym_orders_raw` → агрегирует `sales_daily` |
| `fetch_ym_supplies.py` | `POST /v2/campaigns/{id}/supply-requests` + `…/items` → агрегирует активные заявки → `supplies_planned` (текущий срез) |
| `fetch_ym_stocks.py` | `POST /v2/campaigns/{id}/offers/stocks` (withTurnover) → пагинация → `ym_stocks_raw` + `stock_marketplace` |
| `forecast_stocks.py` | Читает snapshot + sales + office + supplies → скорости 7/14/30 → `days_to_oos` → `forecast` + TG |
| `sheets_helpers.py` | **Всегда импортировать отсюда** `batch_delete_rows`, `safe_append_rows`, `with_retry` |

---

## 4. Ключевые решения (то, что НЕ очевидно из кода)

### 4.1. Две разные метрики продаж — две разные цели

В `sales_daily` два числа на каждую дату-SKU:

- **`qty_sold_created`** — по `date_created`, исключены `CANCELLED/UNPAID/RESERVED/PLACING`, без fake.
  → **Используется в прогнозе.** Опережает выкуп, точнее показывает текущий спрос.
- **`qty_sold_delivered`** — по `date_delivered`, только `DELIVERED`.
  → **Используется в отчётности.** Точная выручка по фактически выкупленному.

Это не дубликат, это разные ответы на «сколько мы продали».

### 4.2. CAMPAIGN_ID ≠ BUSINESS_ID

Это **разные идентификаторы**, оба нужны:

- `YM_CAMPAIGN_ID` (магазин) — для остатков, `/v2/campaigns/{id}/...`
- `YM_BUSINESS_ID` (кабинет/бизнес-аккаунт) — для заказов, `/v1/businesses/{id}/...`

Оба берутся в **Кабинет → Настройки → API и модули**. Самая частая ошибка установщика — перепутать.

### 4.3. Идемпотентность через upsert

Все три скрипта **можно безопасно запускать повторно на ту же дату** — старые строки удаляются, новые пишутся. Это критично:
- cron упал → запустили руками → данные не дублируются.
- backfill за прошлый период → перезапишет аккуратно.

### 4.4. Скорость продаж: 14-дневное окно

В прогнозе используется **`speed_14d_ordered`** (по `qty_sold_created`). Это компромисс:
- 7д — слишком шумно, реагирует на каждый всплеск.
- 30д — слишком инертно, поздно замечает рост спроса.
- 14д — баланс. Параметр `SPEED_USED_HORIZON = 14` в `forecast_stocks.py`.

### 4.5. `stock_office` — необязательный лист

`forecast_stocks.py` пытается прочитать `stock_office` (товары, готовые к отгрузке из офиса). **Если листа нет — `ready_office = 0` для всех позиций.** Это норма на стадиях 1-2; лист появляется только когда есть отдельный модуль офисного учёта.

### 4.6. `supplies_planned` — текущий срез, без истории

Модуль 3 (`fetch_ym_supplies.py`) пишет в `supplies_planned` **только «сейчас»**, не накапливая историю по дням. При каждом запуске старые строки YM удаляются и пишутся свежие. Причины:

- API не отдаёт исторические состояния заявок — только текущее. Записывать вчерашний срез смысла нет, он уже устарел.
- Лист используется только `forecast_stocks.py` как «сколько ещё едет» — для прогноза нужна одна цифра, а не временной ряд.
- Если понадобится динамика поставок (модуль 3.1) — добавится отдельный `ym_supplies_raw` с `fetch_timestamp`-ключом, как уже сделано для остатков.

**Какие заявки попадают в срез:**
- Только тип `SUPPLY` (поставки). `WITHDRAW` (вывозы) и `UTILIZATION` (утилизация) игнорируются — для прогноза остатков они отрицательны и требуют отдельной логики.
- Статусы **кроме** `FINISHED` (уже на складе — это уже остаток), `CANCELLED`, `INVALID`, `CANCELLATION_REQUESTED`. `CREATED` (создана, но не одобрена) **учитываем** — это уже план поставки.
- `qty_in_transit = max(planCount − factCount, 0)` — то что реально едет.
- `nearest_arrival_date` — минимум `targetLocation.requestedDate` по всем заявкам где встречается этот SKU.

### 4.7. Авто-обнаружение новых складов Яндекса

`fetch_ym_stocks.py` перед основным запросом дёргает `GET /v2/warehouses` и **дописывает новые склады в `mp_warehouses`** с пометкой «авто-добавлен ботом». Никаких ручных обновлений справочника.

### 4.8. Формула прогноза учитывает три компонента

```
days_to_oos = (qty_full + ready_office + qty_in_transit) / speed_used
```

- `qty_full` — то что лежит на складе МП (из `stock_marketplace`).
- `ready_office` — то что готово в офисе к отгрузке (из `stock_office`, 0 если листа нет).
- `qty_in_transit` — то что уже едет на склад МП (из `supplies_planned`, 0 если листа нет).

Это **плоский прогноз** — он отвечает «хватит ли всего наличного и идущего, и на сколько дней». **Он НЕ учитывает, что поставка приедет не сегодня.** Если остаток упадёт до 0 через 8 дней, а поставка придёт через 12, формально OOS будет на 4 дня — но плоский прогноз покажет суммарные 18 дней.

Это сделано осознанно: 50 SKU, ручные правки в Sheets, человек принимает решение. Когда понадобится точная временная модель — заведём отдельную колонку `days_to_oos_strict`, текущую оставим как «сколько всего».

При расчёте `recommended_qty` мы вычитаем `qty_in_transit` (а не только `qty_full + ready_office`) — иначе бы рекомендовали заказывать дубль того, что уже едет.

---

## 5. Известные проблемы и как они решены (см. `CHANGES.md`)

Проект уже пережил один патч. Если что-то падает — сначала смотри сюда:

### Проблема 1: `inf` ломает JSON-сериализацию Sheets API

**Симптом:** `ValueError: Out of range float values are not JSON compliant: inf`.
**Причина:** в `stock_marketplace` затесалось `inf` (например, формула `=10/0`), и оно пошло в `append_rows()` без проверки.
**Решение:** `sheets_helpers.sanitize_value()` — на каждую ячейку перед записью. `inf/nan/None → ""`, значения `> 1e15 → ""`.

### Проблема 2: Квота Google Sheets 60 write/min

**Симптом:** `APIError [429]: Quota exceeded for 'Write requests per minute per user'`.
**Причина:** удаление строк в цикле — `for i in reversed(to_delete): ws.delete_rows(i)`. 1500 строк = 1500 HTTPS-запросов = квота выжигается за секунды.
**Решение:** `sheets_helpers.batch_delete_rows()` — группирует смежные индексы в диапазоны и шлёт **один** `spreadsheets.batchUpdate`. 1500 строк → 1 запрос.

### Защита на случай если квота всё-таки кончилась

`sheets_helpers.with_retry()` — экспоненциальный backoff на 429/503 (5/10/20/40/60 сек, до 6 попыток). Используется внутри `batch_delete_rows` и `safe_append_rows` автоматически.

### ❗ Главное правило для будущих изменений

**Любая запись/удаление в Sheets идёт ТОЛЬКО через `sheets_helpers.py`.**
Не вызывать напрямую `ws.append_rows()`, `ws.delete_rows()`, `ws.update()` без обёртки — нарвёшься на те же грабли.

---

## 6. Cron — расписание и логи

```cron
30 5 * * *  cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python fetch_ym_orders.py    >> /var/log/openclaw/ym_orders.log    2>&1
45 5 * * *  cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python fetch_ym_supplies.py  >> /var/log/openclaw/ym_supplies.log  2>&1
0  6 * * *  cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python fetch_ym_stocks.py    >> /var/log/openclaw/ym_stocks.log    2>&1
30 6 * * *  cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python forecast_stocks.py    >> /var/log/openclaw/forecast.log     2>&1
```

| Время | Скрипт | Что пишет | Куда логи |
|---|---|---|---|
| 05:30 | `fetch_ym_orders.py` | `ym_orders_raw`, `sales_daily` | `/var/log/openclaw/ym_orders.log` |
| 05:45 | `fetch_ym_supplies.py` | `supplies_planned` (текущий срез) | `/var/log/openclaw/ym_supplies.log` |
| 06:00 | `fetch_ym_stocks.py` | `ym_stocks_raw`, `stock_marketplace`, обновляет `mp_warehouses` | `/var/log/openclaw/ym_stocks.log` |
| 06:30 | `forecast_stocks.py` | `forecast` + Telegram-сводка | `/var/log/openclaw/forecast.log` |

Все четыре скрипта возвращают **exit-code 1 при ошибке** — cron-обёртка может это ловить.

---

## 7. Окружение (`.env`)

Файл `/opt/openclaw/secrets/.env`, chmod 600, owner `openclaw`:

```bash
# Яндекс.Маркет
YM_API_KEY=<токен с правами: остатки, склады, заказы, FBY-заявки>
YM_CAMPAIGN_ID=<число, магазин>
YM_BUSINESS_ID=<число, бизнес-аккаунт — другой ID>

# Google Sheets
GSHEET_ID=<из URL таблицы>
GOOGLE_CREDS=/opt/openclaw/secrets/gcp-sa.json

# Telegram
TG_BOT_TOKEN=<от BotFather>
TG_ADMIN_CHAT_ID=<chat_id руководителя, через @userinfobot>
```

Шаблон — в `.env.example`. **В git не коммитим ни `.env`, ни `gcp-sa.json`** (они в `.gitignore`).

### Права API-ключа Яндекса (важно для модуля 3)

Для модуля 3 нужен дополнительный scope **`supplies-management:read-only`** («Получение информации по FBY-заявкам»). Базовому ключу, созданному для модулей 1-2, его НЕ выдают автоматически — нужно либо отредактировать существующий ключ, либо создать новый. Если права нет — `fetch_ym_supplies.py` упадёт с HTTP 403 и сообщит об этом в Telegram. Модули 1, 2, 4 при этом продолжат работать.

---

## 8. Что НЕ делает проект (на будущее)

Это **намеренно** оставлено за пределами скоупа:

- **Возвраты** (`qty_returned` всегда 0) — модуль 2.1.
- **Комиссии и удержания МП** — отдельный отчётный API, модуль 3.
- **История поставок** (динамика `supplies_planned` во времени) — модуль 3.1 при необходимости.
- **Точная временная модель прогноза** (отдельный `days_to_oos_strict` с учётом дат поставок) — расширение `forecast_stocks.py`.
- **Wildberries** — модуль 5 (структура та же, API другой, прогноз уже умеет любой marketplace).
- **Расчёт ЗП/KPI сотрудников** — модуль 6.
- **Информация о покупателях** — отдельный `GET /buyer`.
- **Парсинг сообщений сотрудников в Telegram** — на стороне OpenClaw.

Если приходит задача из этого списка — это **новый модуль**, не патч к существующим.

---

## 9. Запуск вручную (шпаргалка)

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher
set -a && source /opt/openclaw/secrets/.env && set +a

# Тест — ничего не пишет в Sheets, только лог
python fetch_ym_orders.py    --dry-run --verbose
python fetch_ym_supplies.py  --dry-run --verbose
python fetch_ym_stocks.py    --dry-run --verbose
python forecast_stocks.py    --dry-run --verbose

# Боевой
python fetch_ym_orders.py
python fetch_ym_supplies.py
python fetch_ym_stocks.py
python forecast_stocks.py

# Backfill (только для orders и stocks — supplies даёт только текущее состояние)
python fetch_ym_orders.py --from 2026-04-01 --to 2026-04-30
python fetch_ym_stocks.py --date 2026-05-23
python forecast_stocks.py --date 2026-05-23
```

---

## 10. Куда смотреть дальше

| Хочешь | Файл |
|---|---|
| Развернуть с нуля | `DEPLOY.md` |
| Понять манифест skill для OpenClaw (cron, триггеры на чат) | `SKILL.md` |
| Узнать про прошлые баги и почему код такой | `CHANGES.md` |
| Изменить логику прогноза | `forecast_stocks.py`, функция `build_forecast` |
| Изменить логику агрегации продаж | `fetch_ym_orders.py`, функция `aggregate_sales` |
| Изменить, какие заявки считаются «в пути» | `fetch_ym_supplies.py`, `EXCLUDED_STATUSES` и `RELEVANT_TYPES` |
| Добавить запись/удаление в Sheets | сначала `sheets_helpers.py`, потом вызывающий код |
| Добавить новый marketplace (WB, Ozon) | новый `fetch_wb_*.py`, `forecast_stocks.py` уже агностичный |

---

## 11. Чек-лист перед коммитом

- [ ] Не закоммитил `.env`, `gcp-sa.json`, `*.json` с ключами?
- [ ] Все записи в Sheets идут через `sheets_helpers` (не напрямую `ws.append_rows`)?
- [ ] `dry-run` прошёл успешно для всех трёх скриптов?
- [ ] Если меняешь схему листов — обновил `SHEET_HEADERS` / парсинг во всех читающих скриптах?
- [ ] `CHANGES.md` обновлён, если был баг?

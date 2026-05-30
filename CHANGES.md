# Changelog

## Patch 2.3 — фильтр warehouse_routing (whitelist)

### Что чинит

В TG-сводках появлялись склады, на которые мы реально не поставляем
(«Домодедово возвратный», «Склад 301», ...). Это шум: Яндекс возвращает
остатки по всем складам Маркета, и `forecast_stocks.py` считал прогноз
для каждой строки `stock_marketplace`, не разбираясь — наш склад
или нет.

### Решение

Новый лист **`warehouse_routing`** с колонками:

| sku | warehouse | active | marketplace | comment |

`forecast_stocks.py` читает его и применяет как **строгий whitelist**:

- Если для SKU есть активные строки routing → прогноз только по этим складам.
- Если SKU нет в routing → **SKU полностью игнорируется** (не в прогнозе, не в TG).
- Если листа нет → фильтр выключен, всё идёт как раньше.
- Если лист пуст → ничего не пройдёт + WARNING в логе.

### Почему whitelist, а не fallback

Сначала был fallback («SKU без routing → берём все склады»), но это даёт
шум: новый SKU автоматически попадает в сводку со всеми его 5-7 складами,
половина из которых нам не нужна. С whitelist — пока не описал SKU явно,
он не показывается. Это намеренно строже.

### Сравнение имён

Регистро- и пробело-нечувствительное (`"Софьино"` == `"софьино"` ==
`" Софьино "`). Это страхует от опечаток в листе.

### Логирование

В журнале появляются строки:
```
warehouse_routing: 42 active pairs for 12 SKUs (YM)
warehouse_routing: filtered out 87 (sku, warehouse) pairs from 12 allowed SKUs;
                   skipped 5 SKUs entirely (not in routing)
```

По SKU-номерам отфильтрованных пар лог НЕ выводит — только итоговый
счётчик. Если нужно отлаживать конкретные пары — добавь временный
`logging.debug` в `build_forecast`.

### Изменённые файлы

- `forecast_stocks.py`:
  - новый метод `read_warehouse_routing()` (возвращает `None` если листа нет,
    `{}` если лист пуст)
  - параметр `routing` в `build_forecast()`
  - в `run()` подключено чтение и прокидывание routing
  - `SHEET_HEADERS` дополнен `"warehouse_routing": "sku"`

### Совместимость

Если листа `warehouse_routing` нет — прогноз работает как раньше, со всеми
складами. Поэтому апдейт безопасно катить сразу; но **сразу после апдейта
нужно создать и заполнить лист**, иначе при следующем шаге настройки можно
сделать невыдающий ничего pipeline.

---

## Patch 2 — qty_full = FIT вместо AVAILABLE + дедуп TG

### Что чинит

**Главный баг прошлой версии:** `fetch_ym_stocks.py` записывал в
`qty_full` только остатки типа `AVAILABLE` («доступный к заказу
прямо сейчас»). Если на складе товар физически есть, но **весь
зарезервирован** под уже размещённые заказы (`FREEZE`), `AVAILABLE = 0` —
и прогноз показывал «остаток 0, OOS через 0 дней» для позиций,
которых физически на складе полно. Яндекс при этом справедливо
показывал `turnover=LOW`, `turnoverDays=100+`.

В TG-сводке это проявлялось так:
```
🔴 Срочно (< 7 дней до OOS):
• ПВ1 / МО Софьино: остаток 0, скорость 1.0/день → ~0 дней
⚠️ Расхождения с оценкой Яндекса:
• ПВ1 / МО Софьино: наш прогноз 0 дн, YM=LOW (323.0 дн)
```

— одна и та же позиция и «срочно заказать!», и «по оценке Яндекса
хватит на год». Раньше блока расхождений не было, поэтому проблему
не замечали.

### Решение

В `fetch_ym_stocks.py` строка:
```python
qty_full=raw.qty_available
```
заменена на:
```python
qty_full=raw.qty_fit  # FIT = AVAILABLE + FREEZE = физический запас
```

Это значит, что в `stock_marketplace` записывается **физический
запас на складе**, включая зарезервированный под открытые заказы.
Зарезервированный товар фактически тоже «в обороте» — он либо уйдёт
покупателю, либо вернётся в `AVAILABLE` при отмене.

### Побочный эффект

Цифры в `stock_marketplace` после апдейта станут выше — на величину
`FREEZE` (зарезервированного). Старые исторические строки остаются
как есть; если строишь графики по истории, увидишь «ступеньку» в
день апдейта. Чтобы её убрать — очисти историю в `stock_marketplace`.
Это необязательно: старые строки не мешают новому прогнозу.

### Бонус: дедуп блока «В пути» в TG

`supplies_planned` хранит одну строку на SKU (агрегат по складам), а
`forecast` — строку на SKU×склад. Из-за этого в TG-сводке один SKU
повторялся в блоке «🚚 В пути на склады YM» столько раз, сколько
у него складов. Добавлена дедупликация по SKU — теперь одна строка
на SKU, и в заголовке указывается общее число уникальных SKU.

### Изменённые файлы

- `fetch_ym_stocks.py` — `qty_full = qty_fit`, обновлены docstrings
- `forecast_stocks.py` — дедуп блока «🚚 В пути» в `build_summary`

---

## Модуль 3 — товары в пути (`fetch_ym_supplies.py`)

### Что добавилось

Новый скрипт `fetch_ym_supplies.py` тянет заявки на поставку FBY через
Partner API и сохраняет «что сейчас едет на склад» в новый лист
`supplies_planned`. `forecast_stocks.py` расширен — учитывает
`qty_in_transit` в формуле `days_to_oos`.

### Решения по бизнес-логике

- **«Активные» заявки** = всё, кроме `FINISHED`, `CANCELLED`,
  `INVALID`, `CANCELLATION_REQUESTED`. Статус `CREATED` (создана,
  но не одобрена) **учитывается** — это уже план поставки.
- **Окно** — все активные, без фильтра по дате (новых заявок мало,
  старые активные интересны не меньше).
- **Только тип `SUPPLY`** — вывозы (`WITHDRAW`) и утилизация
  (`UTILIZATION`) для прогноза остатков отрицательны и считаются
  отдельно (пока вне скоупа).
- **Текущий срез, без истории** — `supplies_planned` переписывается
  целиком при каждом запуске. Если понадобится динамика поставок —
  заведём `ym_supplies_raw` по аналогии со `ym_stocks_raw`.

### Формула

```
qty_in_transit = sum(planCount − factCount) по активным заявкам, где SKU встречается
nearest_arrival_date = min(targetLocation.requestedDate) по тем же заявкам
days_to_oos = (qty_full + ready_office + qty_in_transit) / speed_used
```

В `recommend_qty` тоже вычитаем `qty_in_transit` — иначе бы
рекомендовали заказывать дубль того, что уже едет.

### Изменённые файлы

- `fetch_ym_supplies.py` — новый
- `forecast_stocks.py` — добавлены поля `qty_in_transit`,
  `nearest_arrival` в `ForecastRow`, метод `read_supplies_in_transit`,
  изменена сигнатура `build_forecast`, в TG-сводке появился блок
  «🚚 В пути»
- `SKILL.md`, `DEPLOY.md`, `CLAUDE.md`, `.env.example` — обновлены
- Cron — добавлена строка на 05:45 (между orders и stocks)

### Требования к API-ключу

Существующий ключ для модулей 1-2 **не работает** для supply-requests
автоматически — нужен дополнительный scope **`supplies-management:read-only`**
(«Получение информации по FBY-заявкам»). Можно:
1. Отредактировать существующий ключ в кабинете и добавить scope.
2. Или создать новый ключ только для модуля 3 (его положить
   в отдельную env-переменную не нужно — `YM_API_KEY` общий).

Если права нет — `fetch_ym_supplies.py` упадёт с HTTP 403,
скажет об этом в Telegram, остальные модули не затронуты.

### Графа форкастa получает новые колонки

К существующему листу `forecast` добавлены **две новые колонки**:
`qty_in_transit` и `nearest_arrival`. Они вставляются между
`ready_office` и `speed_7d` — порядок см. в `ForecastRow.as_list()`.

**ВНИМАНИЕ при апдейте на работающий сервер:** перед первым запуском
обновлённого `forecast_stocks.py` нужно вручную добавить эти две
колонки в шапку листа `forecast` (или очистить лист — он будет
перенаполнен). Если не сделать — порядок колонок поедет.

---

## Patch 1 — устранение runtime ошибок

### Симптомы

1. `forecast_stocks.py` падал с `ValueError: Out of range float values
   are not JSON compliant: inf` при попытке записать прогноз в Sheets.
2. `fetch_ym_orders.py` падал с `APIError [429]: Quota exceeded for
   'Write requests per minute per user'` — упирался в квоту Google Sheets,
   когда удалял десятки строк построчно.

### Причины

1. **`inf` в данных.** При чтении из stock_marketplace значение
   `turnover_days` могло прийти как `inf` (например, если в Sheets
   была формула `=10/0`), и без санитизации попадало напрямую в
   JSON-сериализатор Sheets API.

2. **Построчное удаление через `delete_rows()` в цикле.** Каждый вызов —
   это отдельный HTTPS запрос к Sheets. Google ограничивает квоту
   60 write-операций в минуту на пользователя. Окно 30 дней по 50 SKU =
   1500 строк = выжигание квоты за секунды.

### Что исправлено

Добавлен модуль `sheets_helpers.py` с тремя ключевыми утилитами:

#### 1. `batch_delete_rows()` — одно удаление вместо N

```python
# Было (медленно, рвёт квоту):
for i in reversed(to_delete):
    ws.delete_rows(i)

# Стало (одним batchUpdate):
batch_delete_rows(ws, to_delete)
```

Группирует смежные индексы в диапазоны и удаляет ВСЕ за один запрос
через `spreadsheets.batchUpdate` API. Удаление 1500 строк = 1 запрос
вместо 1500.

#### 2. `safe_append_rows()` — санитизация перед записью

```python
# Было:
ws.append_rows([r.as_list() for r in rows],
               value_input_option="USER_ENTERED")

# Стало:
safe_append_rows(ws, [r.as_list() for r in rows])
```

Внутри проходит по каждой ячейке через `sanitize_value()`:
- `inf` / `-inf` → ""
- `nan` → ""
- `None` → ""
- значения > 1e15 → "" (с warning в лог)

#### 3. `with_retry()` — backoff на 429

Если несмотря на батчинг всё-таки упрёмся в квоту (например, несколько
скриптов запустились одновременно) — автоматический retry с
экспоненциальной паузой 5/10/20/40/60 секунд.

### Дополнительные улучшения в forecast_stocks.py

- `_safe_int()` / `_safe_float()` — парсинг числовых значений из Sheets
  с автоматической отбраковкой `inf`/`nan`/мусорных строк.

- Защита расчёта `days_to_oos`:
  ```python
  # Было:
  if speed_used > 0:
      days = int(round((qty + ready) / speed_used))
  # Если speed_used = 1e-300, deli даёт inf

  # Стало:
  if speed_used >= 0.01:  # явный порог
      raw = (qty + ready) / speed_used
      days = int(round(raw)) if raw < 10000 else 9999
  ```

  Дополнительная отсечка переполнения через `raw < 10000`.

### Затронутые файлы

- `sheets_helpers.py` — новый общий модуль
- `fetch_ym_orders.py` — два upsert метода переведены на batch
- `fetch_ym_stocks.py` — upsert_snapshot_rows + append_raw_rows
- `forecast_stocks.py` — чтение + write_forecast + расчёт days_to_oos

### Установка обновлений на работающий сервер

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher
# Распаковать новый zip поверх (старые файлы будут заменены)
unzip -o ~/ym_stocks_fetcher_patched.zip

# Проверить, что появился новый файл
ls -la sheets_helpers.py

# Тест: dry-run всех трёх скриптов
source /opt/openclaw/secrets/.env
python forecast_stocks.py --dry-run --verbose
python fetch_ym_stocks.py --dry-run --verbose
python fetch_ym_orders.py --dry-run --verbose

# Если dry-run прошли — боевой запуск
python forecast_stocks.py
```

Если квота уже выжжена сегодняшними неуспешными попытками — подождать
60 секунд перед следующим запуском, либо `with_retry` сам подождёт.

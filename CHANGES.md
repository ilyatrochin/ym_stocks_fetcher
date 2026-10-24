# Changelog

## Модуль 7.1 — FBS Wildberries, этап 3 (сдача в ПВЗ — ЗАПИСЬ в WB, необратимо)

- ПВЗ задаётся адресом или ID: `FBS_WB_SHIPPING_POINT="Москва, проспект Лихачёва, 22"`;
  бот ищет его среди пунктов отгрузки поставки (`officeType=pp`, сегодня) по адресу/названию
  (ё/е, «пр-кт»/«проспект», номер дома целиком). Несколько/похожие — кнопки; перегружен — отказ.
- «🚛 Сдать в ПВЗ» (появляется, когда есть грузоместа) и `/fbs_wb_deliver`: проверки (открыта,
  для ПВЗ, есть места, маркировка) → подтверждение с ПВЗ → `shipping-method` (сами, сегодня;
  ошибки из `results[]`) → `deliver` (409 — по-русски) → QR 58×40 в чат и Drive.
- Подтверждение «привязано» к увиденному: если после него в поставке изменилось число
  заданий/мест или сменился день — отказ, подтвердить заново.
- `/fbs_wb_qr`, «🖨 QR поставки» — QR уже сданной поставки. CLI: `--points`, `--deliver [--point]
  [--yes]` (без `--yes` — только проверка), `--qr`.
- Новые env: `FBS_WB_SHIPPING_POINT`, опц. `FBS_WB_CITY`.

## Модуль 7.1 — FBS Wildberries, этап 2 (сборка поставки и грузоместа — ЗАПИСЬ в WB)

- «📦 Собрать поставку»: все новые задания склада → поставка по группам
  (auto: открытая с теми же признаками / своя пустая «FBS WB <склад> …» / новая;
  `new`; `supply=WB-GI-…`). Добавление пачками по 100; если WB отклонил пачку —
  по одному, отклонённые показываются с причиной. Повторное нажатие дублей не создаёт.
- Документы по каждой открытой поставке склада: стикеры заданий PNG 58×40 → PDF
  в порядке листа сборки, лист сборки с подписями стикеров → чат и своя папка
  Drive `GDRIVE_FBS_WB_FOLDER_ID/YYYY-MM-DD/`.
- Затем «сейчас N шт — сколько грузомест?» (кнопки 1…min(6, N//2),
  `/fbs_wb_boxes WB-GI-… K`): число мест доводится до K (создать/удалить),
  стикеры мест 58×40; «🖨 Перепечатать» — без изменения числа.
- Поставки «не для ПВЗ» (`isPickupPointShipmentAllowed=false`) — без вопроса про
  грузоместа и с отказом в `/fbs_wb_boxes`; признаки ПВЗ/B2B заданий на сборке
  берутся с поставки (в `/api/v3/orders` их нет).
- Запись только для `TG_ALLOWED_USERS` (пустой список — никому); блокировка от
  двойного нажатия. CLI: `--take [--mode] [--supply]`, `--boxes WB-GI-…:K`, `--no-drive`.
- Новые env: `GDRIVE_FBS_WB_FOLDER_ID`, опц. `FBS_WB_SUPPLY_MODE`, `FBS_WB_STICKER`.

## Модуль 7.1 — FBS Wildberries, этап 1 (`/fbs_wb`, только чтение)

Новая функциональность, отдельная от FBS Яндекса (`fbs_shipment.py` и
`fbs_bot_handlers.py` не менялись, импортов между модулями нет). Проект —
`DESIGN_FBS_WB.md`.

- `fbs_wb.py` — склады продавца, новые задания и открытые поставки → план по
  складу (без даты: собирается всё новое), группы поставок
  (cargoType/crossBorder/B2B), отменённые в поставке, маркировка; лист сборки PDF;
  CLI `--warehouse`, `--list-warehouses`, `--explain`, `--fixture`.
- `fbs_wb_bot.py` — `/fbs_wb [склад]`, выбор склада кнопками, «Лист сборки»,
  `/fbs_wb_check`. Префикс callback `fbswb:`.
- `tg_supplies_bot.py` — регистрация WB только при `WB_API_TOKEN`; ошибка
  загрузки WB логируется и не роняет бота.
- Новые env: `WB_API_TOKEN` (категория «Маркетплейс»), опц. `FBS_WB_WAREHOUSE`,
  `WB_SANDBOX=1`.

## Модуль 7 — FBS-отгрузка на сегодня (`/fbs` в боте)

Новая функциональность, не патч. Отдельно от FBY-прогноза.

- `fbs_shipment.py` — FBS-заказы с отгрузкой сегодня → лист сборки (PDF,
  reportlab) + ярлыки 58×40 от Маркета (PDF) → общий диск Google Drive.
- `fbs_bot_handlers.py` — `/fbs`: сводка по SKU, кнопки «Документы»
  (PDF в чат + Drive), «Собрано» (STARTED → READY_TO_SHIP с подтверждением).
- `tg_supplies_bot.py` — регистрация `/fbs`; старый `CallbackQueryHandler`
  ограничен `pattern=^(pack|unpack):`, чтобы не перехватывать FBS-кнопки.
- После «Документы» заказы листа автоматически переводятся в READY_TO_SHIP
  (`FBS_AUTO_READY=1` по умолчанию; только при полученных ярлыках).
- `/fbs [дата]`, блок «Не в листе» с причинами, `/fbs_check <id>`.
- Новые env: `GDRIVE_FBS_FOLDER_ID`, `FBS_AUTO_READY`, `FBS_LABEL_FORMAT`, опц. `FBS_TZ`,
  `FBS_FONT_PATH`. Новая зависимость `reportlab`, системная — `fonts-dejavu-core`.


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

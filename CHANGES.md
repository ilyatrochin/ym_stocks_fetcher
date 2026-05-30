# Changelog

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

# Sales History Pipeline (v7)

Объединение свежих заказов из API (последние 30 дней) с историческими
из Reports API (старше 30 дней).

## Что появилось

1. **`parse_realization.py`** — парсер XLSX-отчёта «Доставленные товары».
2. **`fetch_ym_report.py`** — теперь умеет команду `--import` (заливка в
   Google Sheets) и `--import-and-download` (всё в одном).
3. **Новый лист `sales_history`** в Google Sheets с структурой:
   ```
   date | sku | qty_delivered | source_month
   ```
4. **`forecast_stocks.py`** — новые функции `compute_history_speed` и
   `blend_speeds`. Сглаживает скорости из ym_orders_raw данными из
   sales_history (с большим весом на свежие).

## Полный pipeline

### Шаг 1 — Залить историю за каждый месяц

```bash
# Сначала надо чтобы лист sales_history появился (создастся автоматически)
# Заливаем каждый прошлый месяц по очереди
for month in 1 2 3 4; do
    python fetch_ym_report.py --year 2026 --month $month --import-and-download
done
```

Каждая команда:
1. Запросит generation у YM
2. Дождётся `DONE` (обычно 10-30 сек)
3. Скачает XLSX
4. Распарсит «Доставленные товары»
5. Запишет агрегаты в `sales_history` (с пометкой source_month)

Если лист sales_history уже содержит строки за этот месяц — они **будут
заменены** (upsert по source_month).

### Шаг 2 — Прогноз с учётом истории

```bash
python forecast_stocks.py
```

В логе появится:
```
sales_history: 5 SKUs, 124 entries, range 2026-01-05..2026-04-30
History speeds computed for 5 SKUs (window 2026-02-25..2026-04-26, 61 days)
Speeds blended with history (weight=0.30). Pairs before=8, after=10
```

`+2 from history-only SKUs` означает что 2 SKU были найдены **только** в
истории (например, временно нет продаж в последние 30 дней) — для них
скорость взята полностью из истории.

## Как работает сглаживание

Формула:
```
blended = max(current, current * 0.7 + historical_per_warehouse * 0.3)
```

- Если **свежая скорость растёт** — она остаётся (защита от занижения).
- Если **свежая скорость провалилась**, но история стабильно высокая —
  blended подтянется вверх.
- Если **SKU есть только в истории** — берётся история целиком,
  делится поровну между складами из routing.

### Настройка веса

```bash
# Больше веса истории (для очень волатильных SKU)
python forecast_stocks.py --history-weight 0.5

# Меньше веса (доверяем последним 30 дням)
python forecast_stocks.py --history-weight 0.1

# Полностью отключить
python forecast_stocks.py --no-history

# Изменить окно истории (по умолчанию 90 дней)
python forecast_stocks.py --history-lookback 180
```

## Импорт без скачивания (если файл уже есть)

```bash
# Например, если уже качали для inspect
python fetch_ym_report.py --import /tmp/ym_reports/goods_realization_2026-04.xlsx

# Dry-run — посмотреть что будет записано, без записи
python fetch_ym_report.py --import /tmp/ym_reports/goods_realization_2026-04.xlsx --dry-run

# Указать месяц явно (если файл с нестандартным именем)
python fetch_ym_report.py --import /path/file.xlsx --source-month 2026-03
```

## Регламент обновления

История зализывается **один раз** для каждого прошлого месяца. После
залива модулю можно дать ей лежать.

**Когда наступает новый месяц** (например, 1 июня) — стоит залить
предыдущий месяц как историю:
```bash
python fetch_ym_report.py --year 2026 --month 5 --import-and-download
```

Это можно положить в cron на 5-е число каждого месяца:
```cron
0 6 5 * *  /opt/.../fetch_ym_report.py --year $(date -d '15 days ago' +\%Y) --month $(date -d '15 days ago' +\%-m) --import-and-download
```

## Что НЕ умеет история

- Нет привязки к складу (Reports API не отдаёт `warehouseId` отгрузки).
  Поэтому история делится поровну между складами из routing.
- Только `qty_delivered` — у вас был выбор «среднее created/delivered»,
  но в отчёте есть **только delivered**. Это нормально: история нужна
  для оценки **тренда**, не для дневной точности.
- Только продажи (тип `Продажа физлицу`/`Продажа юрлицу`). Возвраты
  не учитываются.

## Проверка

```bash
# Самодиагностика парсера на скачанном файле
python parse_realization.py /tmp/ym_reports/goods_realization_2026-04.xlsx
```

Выведет статистику: сколько строк, по каждому SKU, общую скорость.

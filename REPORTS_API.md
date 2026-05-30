# Reports API — пошаговый план

## Что мы выяснили

Endpoint `POST /v1/businesses/{id}/orders` **полностью игнорирует фильтр
по датам** и возвращает только последние ~30 дней — это видно по логу:

```
DEBUG page 1: 50 orders, creationDate range 2026-05-21..2026-05-26
                                       (requested 2026-01-02..2026-01-31)
```

Это поведение самого API, не баг скрипта. Чтобы достать историю — нужен
другой endpoint: `POST /reports/goods-realization/generate`.

**Хорошая новость:** за месяц у вас 322 уникальных заказа (видно в логе:
`Unique orders: 322`). Это уже неплохой объём для прогноза с горизонтом
14/30 дней. Если бэкфилл истории окажется сложным, можно жить с тем что
есть и ждать пока накопится больше.

## Новый модуль: fetch_ym_report.py

Это **отдельный** скрипт. Он работает с Reports API асинхронно:
1. Просит сгенерировать отчёт за месяц
2. Ждёт пока YM соберёт XLSX
3. Скачивает файл

Запись в Google Sheets пока **не делается** — потому что я не знаю
точную структуру колонок отчёта. Сначала надо посмотреть один файл.

## Шаг 1 — Установить openpyxl

```bash
pip install openpyxl
# или
pip install -r requirements.txt
```

## Шаг 2 — Скачать тестовый отчёт и посмотреть структуру

```bash
python fetch_ym_report.py --year 2026 --month 4 --inspect -v
```

Что будет:
1. POST на generate → получим `reportId`
2. Скрипт каждые 10 сек проверяет готовность (`status=DONE`)
3. Скачивает XLSX в `/tmp/ym_reports/goods_realization_2026-04.xlsx`
4. Распечатывает структуру: листы, заголовки, первые 8 строк

Ожидаемый вывод (например):
```
2026-05-26 12:00:00 [INFO] POST /reports/goods-realization/generate body={'businessId': 176253667, 'year': 2026, 'month': 4}
2026-05-26 12:00:01 [INFO] Report generation started: reportId=abc123def456
2026-05-26 12:00:01 [INFO] [poll #1] status=PROCESSING
2026-05-26 12:00:11 [INFO] [poll #2] status=PROCESSING
2026-05-26 12:00:21 [INFO] [poll #3] status=DONE
2026-05-26 12:00:21 [INFO] Saved /tmp/ym_reports/goods_realization_2026-04.xlsx (12345 bytes)

=== goods_realization_2026-04.xlsx ===
Листы: ['Реализация']

--- Лист «Реализация» (350 строк × 12 колонок) ---
  стр.1: ['Отчёт по реализации за апрель 2026', '', ...]
  стр.2: ['']
  стр.3: ['Дата', 'SKU', 'Название', 'Кол-во', 'Цена', ...]
  стр.4: ['2026-04-01', 'ЧХ6', 'Чехол для ноутбука', '3', '634', ...]
  ...
```

## Шаг 3 — Прислать мне вывод

После выполнения шага 2 пришлите:
1. **Вывод раздела `=== goods_realization_*.xlsx ===`** — то есть листы,
   заголовки и первые строки данных
2. Если хотите — сам файл `/tmp/ym_reports/goods_realization_2026-04.xlsx`,
   тогда я смогу проверить структуру всех колонок

После этого я напишу:
- Парсер XLSX → строки нужного формата
- Запись в отдельный лист `sales_history_report` в вашем Google Sheets
  (отдельный, потому что там агрегаты, а не сырые заказы — структура
  не совпадает с `ym_orders_raw`)
- Логику в `forecast_stocks.py`: если в `ym_orders_raw` нет данных
  старше 30 дней — добирать недостающее из `sales_history_report`

## Команды-шпаргалка

```bash
# Полный цикл: скачать апрель и посмотреть
python fetch_ym_report.py --year 2026 --month 4 --inspect

# Только сгенерировать, не ждать (вернёт reportId)
python fetch_ym_report.py --year 2026 --month 3 --generate-only

# Скачать когда будет готов
python fetch_ym_report.py --report-id <id-из-предыдущего-шага> --download-only --inspect

# Альтернатива: указать произвольные даты вместо месяца
python fetch_ym_report.py --from 2026-01-15 --to 2026-03-15 --inspect

# Скачать сразу несколько месяцев
for m in 1 2 3 4; do
    python fetch_ym_report.py --year 2026 --month $m
done
```

## Возможные ошибки

- `HTTP 400` — попробуйте альтернативный формат: `--from` / `--to` вместо
  `--year` / `--month`. Возможно endpoint требует один из вариантов.
- `Report failed: status=FAILED` — пришлите весь лог, попробуем разобраться
  что не нравится API.
- `Timeout after 600s` — отчёт делается дольше 10 минут. Увеличьте таймаут
  или используйте `--generate-only` + позже `--download-only`.

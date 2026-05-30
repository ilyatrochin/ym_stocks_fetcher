---
name: ym_stocks_fetcher
description: "Скачивает остатки, оборачиваемость и заказы с Яндекс.Маркет (FBY) через Partner API, пишет в Google Sheets и формирует прогноз дней до OOS. Запускается по cron: 05:30 заказы, 06:00 остатки, 06:30 прогноз. Реагирует на запросы в Telegram ('покажи остатки', 'продажи за неделю', 'когда заказывать ЧХ5')."
trigger_keywords: [остатки, прогноз, OOS, оборачиваемость, продажи, заказы, выкуп, Яндекс, YM]
schedule:
  - cron: "30 5 * * *"
    command: "python /opt/openclaw/skills/ym_stocks_fetcher/fetch_ym_orders.py"
  - cron: "0 6 * * *"
    command: "python /opt/openclaw/skills/ym_stocks_fetcher/fetch_ym_stocks.py"
  - cron: "30 6 * * *"
    command: "python /opt/openclaw/skills/ym_stocks_fetcher/forecast_stocks.py"
---

# YM Fetcher (модули 1 + 2)

Объединённый skill: остатки, заказы и прогноз для Яндекс.Маркет (FBY).

## Состав

### Модуль 1: остатки + прогноз
- `fetch_ym_stocks.py` — POST /v2/campaigns/{id}/offers/stocks
- `forecast_stocks.py` — расчёт дней до OOS

### Модуль 2: заказы → продажи
- `fetch_ym_orders.py` — POST /v1/businesses/{id}/orders

## Расписание

| Время | Что происходит |
|---|---|
| **05:30** | fetch_ym_orders → ym_orders_raw + sales_daily |
| **06:00** | fetch_ym_stocks → ym_stocks_raw + stock_marketplace |
| **06:30** | forecast_stocks → forecast + сводка в Telegram |

30 минут между шагами — на случай долгого ответа API.

## Окружение

`/opt/openclaw/secrets/.env`:

```bash
YM_API_KEY=<токен с правами: остатки, склады, заказы>
YM_CAMPAIGN_ID=<для модуля 1>
YM_BUSINESS_ID=<для модуля 2>
GSHEET_ID=<ID таблицы>
GOOGLE_CREDS=/opt/openclaw/secrets/gcp-sa.json
TG_BOT_TOKEN=<токен от BotFather>
TG_ADMIN_CHAT_ID=<chat_id руководителя>
```

CAMPAIGN_ID (магазин) ≠ BUSINESS_ID (кабинет) — это разные идентификаторы,
оба находятся в Кабинет → Настройки → API и модули.

## Ключевые решения

### Модуль 1: остатки
- Идемпотентность: повтор на ту же дату перезаписывает.
- 7 типов остатков в ym_stocks_raw, только AVAILABLE в stock_marketplace.
- Авто-обнаружение новых складов через GET /v2/warehouses.

### Модуль 2: заказы
- **Две метрики продаж:**
  - `qty_sold_created` по date_created — для прогноза (опережает выкуп).
  - `qty_sold_delivered` по date_delivered — для отчётности (точная выручка).
- Окно 30 дней (максимум API). Записи старше окна не трогаются.
- Один заказ с N позициями → N строк в ym_orders_raw.
- Отменённые, неоплаченные, fake — НЕ идут в qty_sold_created.
- Скидки платформы (subsidies) вычитаются из выручки.

### Модуль 3 (forecast)
- speed_14d_ordered (баланс шума и инерции).
- Учитывает stock_office.ready_to_ship_ym (если лист есть).
- Sanity-check против turnover Яндекса.

## Что НЕ делают (расширения)

- Возвраты — модуль 2.1.
- Комиссии и расходы МП — модуль 3 (отдельный отчётный API).
- Wildberries — модуль 4.
- Информация о покупателях — отдельный GET /buyer.

## Запуск

```bash
# Тест
python fetch_ym_orders.py --dry-run --verbose
python fetch_ym_stocks.py --dry-run --verbose
python forecast_stocks.py --dry-run --verbose

# Боевой
python fetch_ym_orders.py
python fetch_ym_stocks.py
python forecast_stocks.py

# Backfill
python fetch_ym_orders.py --from 2026-04-01 --to 2026-04-30
python fetch_ym_stocks.py --date 2026-05-23
```

## Триггеры от пользователя в чате

- «продажи за неделю» → читает sales_daily за 7 дней
- «сколько заказов вчера» → ym_orders_raw за вчера
- «процент отмен» → агрегирует ym_orders_raw по статусам
- «запусти sync» → fetch_ym_orders --days=1 + fetch_ym_stocks
- «остатки ЧХ5» → stock_marketplace для SKU
- «backfill за апрель» → fetch_ym_orders --from=... --to=...

## Зависимости

```
pip install requests gspread oauth2client python-dotenv
```

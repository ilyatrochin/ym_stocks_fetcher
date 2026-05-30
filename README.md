# ym_stocks_fetcher

Skill для [OpenClaw](https://github.com/openclaw/openclaw): автоматический сбор остатков, продаж, **товаров в пути** и прогноза OOS с Яндекс.Маркет (FBY) в Google Sheets + алерты в Telegram.

> 👉 **Для быстрого погружения читай [`CLAUDE.md`](./CLAUDE.md)** — единая точка входа.
> 👉 Для установки на сервер — [`DEPLOY.md`](./DEPLOY.md).

---

## Что делает

| Время | Скрипт | Результат |
|---|---|---|
| 05:30 | `fetch_ym_orders.py` | Заказы за 30 дней → `ym_orders_raw` + `sales_daily` |
| 05:45 | `fetch_ym_supplies.py` | Активные FBY-заявки на поставку → `supplies_planned` (текущий срез) |
| 06:00 | `fetch_ym_stocks.py` | Остатки + оборачиваемость по всем складам → `ym_stocks_raw` + `stock_marketplace` |
| 06:30 | `forecast_stocks.py` | Прогноз дней до OOS с учётом товаров в пути + сводка в Telegram → `forecast` |

Всё пишется в одну Google Sheets-таблицу руководителя — это рабочее место и UI одновременно.

## Стек

- Python 3.10+
- `requests` — HTTP к Партнёрскому API Яндекс.Маркета
- `gspread` + `oauth2client` — запись в Google Sheets
- `python-dotenv` — окружение

Никаких баз данных, никакой админки. Sheets — источник правды.

## Быстрый старт (локальный тест)

```bash
git clone <repo-url> ym_stocks_fetcher
cd ym_stocks_fetcher

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# заполнить .env (см. DEPLOY.md, шаги 2-4)

set -a && source .env && set +a

python fetch_ym_orders.py    --dry-run --verbose
python fetch_ym_supplies.py  --dry-run --verbose
python fetch_ym_stocks.py    --dry-run --verbose
python forecast_stocks.py    --dry-run --verbose
```

`--dry-run` не пишет в Sheets, только логирует — безопасный первый запуск.

## Структура

```
.
├── CLAUDE.md             обзор проекта (читать первым)
├── SKILL.md              манифест для OpenClaw
├── DEPLOY.md             развёртывание на сервер
├── CHANGES.md            changelog патчей
│
├── fetch_ym_orders.py    заказы → продажи
├── fetch_ym_supplies.py  заявки на поставку → товары в пути
├── fetch_ym_stocks.py    остатки + оборачиваемость
├── forecast_stocks.py    прогноз days_to_oos с учётом транзита
├── sheets_helpers.py     общие утилиты (batch-операции, retry, санитизация)
│
├── requirements.txt
└── .env.example
```

## Конфигурация

Скрипты читают переменные окружения (см. `.env.example`):

- `YM_API_KEY`, `YM_CAMPAIGN_ID`, `YM_BUSINESS_ID` — Яндекс.Маркет Partner API
- `GSHEET_ID`, `GOOGLE_CREDS` — Google Sheets + сервисный аккаунт
- `TG_BOT_TOKEN`, `TG_ADMIN_CHAT_ID` — уведомления

`CAMPAIGN_ID ≠ BUSINESS_ID` — это разные идентификаторы, оба находятся в **Кабинет → Настройки → API и модули**.

Для модуля 3 API-ключу нужен дополнительный scope **`supplies-management:read-only`** («Получение информации по FBY-заявкам»).

## Что осознанно НЕ делает

- Возвраты (отдельный модуль 2.1)
- Комиссии и удержания МП (модуль будущего)
- История поставок (динамика во времени; сейчас только текущий срез)
- Wildberries / Ozon (модули 5+)

Расчёт прогноза уже агностичен к маркетплейсу — добавление WB сводится к новому `fetch_wb_*.py`.

## Лицензия

См. [LICENSE](./LICENSE).

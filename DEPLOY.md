# Модуль 1: YM Stocks Fetcher — деплой

Пошаговая инструкция для развёртывания первого модуля на сервере с OpenClaw.

Время: ~1-2 часа на первоначальную настройку.

---

## Шаг 1. Подготовить шаблон в Google Sheets (15 мин)

1. Залить файл `warehouse_template_v2.xlsx` в Google Drive.
2. Правой кнопкой → «Открыть с помощью» → «Google Таблицы».
3. Файл сохранится как Google Sheet — скопируйте его ID из URL:
   `docs.google.com/spreadsheets/d/`**`<вот этот кусок>`**`/edit`
4. Заполнить лист `products`: добавьте все актуальные SKU. Колонка `sku_ym`
   должна точно совпадать с offerId, который вы видите в кабинете
   Яндекс.Маркет (это правильное название SKU, например "ПВ1" или "пв1").

Тестовые строки можно оставить — они не мешают, но при первом боевом fetch
будут перезаписаны.

---

## Шаг 2. Получить ключи Яндекс.Маркет (10 мин)

1. Войти в [кабинет продавца](https://partner.market.yandex.ru/).
2. Иконка аккаунта → **Настройки** → слева **API и модули**.
3. Скопировать **Идентификатор кампании** (числовой).
4. Вкладка **API-ключи** → **Создать новый ключ**:
   - Имя: `openclaw-stocks-fetcher`
   - Доступы: ✅ **Остатки и оборачиваемость**, ✅ **Информация о складах**
   - Срок: максимальный
5. Скопировать ключ сразу (показывается один раз).

---

## Шаг 3. Сервисный аккаунт Google Cloud (15 мин)

1. [Google Cloud Console](https://console.cloud.google.com/) → создать проект
   (или взять существующий).
2. **APIs & Services** → **Enable APIs**: включить **Google Sheets API** и
   **Google Drive API**.
3. **IAM & Admin** → **Service Accounts** → **Create Service Account**:
   - Name: `openclaw-sheets`
   - Role: можно без роли проекта, права даём на конкретный Sheet
4. Created → вкладка **Keys** → **Add Key** → **JSON** → скачать файл.
5. Открыть JSON, найти поле `client_email` (что-то вроде
   `openclaw-sheets@project-xxx.iam.gserviceaccount.com`).
6. В Google Sheet (нашем шаблоне) → **Поделиться** → добавить этот email
   как **Редактора**.

---

## Шаг 4. Telegram бот (5 мин)

1. Написать [@BotFather](https://t.me/BotFather) → `/newbot` → задать имя
   и username → получить токен.
2. Написать вашему боту что-нибудь (любое сообщение).
3. Написать [@userinfobot](https://t.me/userinfobot) — он покажет ваш
   chat_id (это нужно для отправки сообщений вам).

---

## Шаг 5. Развернуть OpenClaw и установить skill (30 мин)

Если OpenClaw ещё не установлен — следуй [официальной инструкции](
https://github.com/openclaw/openclaw#install). После установки:

```bash
# Создать папку skills
sudo mkdir -p /opt/openclaw/skills /opt/openclaw/secrets
sudo chown -R openclaw:openclaw /opt/openclaw

# Скопировать skill (все 5 файлов)
cd /opt/openclaw/skills
sudo -u openclaw git clone <ваш-репо>/ym_stocks_fetcher
# или вручную: scp папку ym_stocks_fetcher/ на сервер

# Установить зависимости
cd /opt/openclaw/skills/ym_stocks_fetcher
sudo -u openclaw pip install -r requirements.txt

# Положить ключ Google
sudo mv ~/Downloads/project-xxx-yyy.json /opt/openclaw/secrets/gcp-sa.json
sudo chmod 600 /opt/openclaw/secrets/gcp-sa.json

# Создать .env
sudo -u openclaw cp .env.example /opt/openclaw/secrets/.env
sudo -u openclaw nano /opt/openclaw/secrets/.env  # заполнить
sudo chmod 600 /opt/openclaw/secrets/.env
```

---

## Шаг 6. Тестовый прогон (10 мин)

**Сначала dry-run** — без записи в Sheet:

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher
set -a && source /opt/openclaw/secrets/.env && set +a
python fetch_ym_stocks.py --dry-run --verbose
```

Должны увидеть в выводе:
- `Pagination complete after N pages`
- `DRY RUN: would write N raw + N snapshot rows`
- Несколько примеров строк

Если ошибка `401 Unauthorized` — неверный API-key или campaignId. Если
`403` — у ключа нет нужного доступа.

**Боевой запуск** (один раз руками):

```bash
python fetch_ym_stocks.py --verbose
```

Открыть Google Sheet → проверить, что в `ym_stocks_raw` и
`stock_marketplace` появились свежие строки. В Telegram должно прийти
сообщение ✅.

**Прогноз** (после первого fetch):

```bash
python forecast_stocks.py --dry-run --verbose
```

Покажет рассчитанные дни-до-OOS. Если первый день — скорости будут
маленькие (исторических продаж в Sheet ещё нет). Это норма.

```bash
python forecast_stocks.py
```

В `forecast` появятся строки на сегодня, в Telegram придёт сводка.

---

## Шаг 7. Cron (5 мин)

```bash
sudo -u openclaw crontab -e
```

Добавить:

```cron
# YM Stocks Fetcher — модуль 1
0 6 * * *   cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python fetch_ym_stocks.py >> /var/log/openclaw/ym_fetch.log 2>&1
30 6 * * *  cd /opt/openclaw/skills/ym_stocks_fetcher && set -a && . /opt/openclaw/secrets/.env && set +a && python forecast_stocks.py >> /var/log/openclaw/forecast.log 2>&1
```

Создать папку для логов:

```bash
sudo mkdir -p /var/log/openclaw
sudo chown openclaw:openclaw /var/log/openclaw
```

---

## Что должно работать после этого

1. Каждое утро 06:00 — свежий снапшот остатков в `stock_marketplace`.
2. Каждое утро 06:30 — прогноз в `forecast` и сводка в Telegram.
3. Можно в любой момент вызвать вручную:
   ```bash
   sudo -u openclaw bash -c 'cd /opt/openclaw/skills/ym_stocks_fetcher && python fetch_ym_stocks.py'
   ```

---

## Чего нет в этом модуле (следующие этапы)

- **Продажи**: пока скорости считаются из листа `sales_daily`, который
  заполняется вручную или другим модулем. Чтобы автоматизировать —
  нужен модуль 2 (`ym_sales_fetcher`).
- **Wildberries**: модуль 3 будет идентичным по структуре, но с другим
  API. Прогноз уже умеет работать с любым marketplace.
- **Парсинг сообщений сотрудников из Telegram**: модуль 4.
- **Расчёт ЗП и КПИ**: модуль 5.

---

## Проблемы и решения

**`HTTP 420 (rate limit)`** — обычно не должна случиться для каталога
&lt;1000 SKU. Если случилось — скрипт сам подождёт и повторит. Если
происходит регулярно, увеличить `RATE_SLEEP` в `fetch_ym_stocks.py`.

**`Pagination complete after 1 pages`, но в Sheet 0 строк** — значит API
вернул пустой ответ. Проверить, что в Яндекс.Маркете остатки есть
(в кабинете) и что campaignId правильный.

**В forecast `speed_used = 0` для всех SKU** — в `sales_daily` нет
данных. Нужно подождать накопления истории или вручную внести продажи.

**Telegram сообщения не приходят** — проверь, что писал боту хотя бы раз;
без этого Telegram блокирует первое сообщение от бота.

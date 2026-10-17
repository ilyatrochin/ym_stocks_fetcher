# Деплой модуля 7 — FBS-отгрузка (`/fbs`)

Займёт ~15 минут. Код приезжает обычным деплоем (push в `main`); `deploy.sh`
сам доустановит `reportlab`, потому что изменился `requirements.txt`.

## 1. Шрифт с кириллицей (один раз, нужен sudo)

```bash
sudo apt install -y fonts-dejavu-core
ls /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf   # должен быть
```

Без шрифта лист сборки не сформируется — бот сообщит об этом явно.

## 2. Права API-ключа Яндекса

Кабинет → Настройки → API и модули → ключ из `YM_API_KEY` → добавить
**«Обработка заказов и учёт товаров» (`inventory-and-order-processing`)** —
полный доступ, не read-only: нужен для кнопки «Собрано» (READY_TO_SHIP).
Без записи `/fbs` и «Документы» работают, «Собрано» вернёт 403.

## 3. Общий диск Google Drive

1. Google Drive → **Общие диски** → создать (например, «FBS отгрузки»).
   Нужен Google Workspace; обычная папка в «Моём диске» **не подойдёт** —
   сервисный аккаунт не может туда загружать (`storageQuotaExceeded`).
2. «Управление участниками» → добавить `client_email` из `gcp-sa.json`
   с ролью **«Менеджер контента»**.
3. Открыть общий диск (или папку внутри него), скопировать ID из URL
   `https://drive.google.com/drive/folders/<ID>`.
4. Убедиться, что в GCP-проекте сервисного аккаунта включён **Google Drive API**.

## 4. `.env`

```bash
GDRIVE_FBS_FOLDER_ID=<ID из шага 3>
FBS_LABEL_FORMAT=A9_HORIZONTALLY   # 58×40
TG_ALLOWED_USERS=<user_id через запятую>   # ОБЯЗАТЕЛЬНО для «Собрано»
```

Кнопка «Собрано» работает **только** при непустом `TG_ALLOWED_USERS`
(в отличие от остальных команд бота, которые при пустом списке открыты всем).

## 5. Проверка и перезапуск бота

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher_patched/ym_stocks_fetcher_v2
set -a && . /opt/openclaw/secrets/.env && set +a
/opt/openclaw/skills/ym_stocks_fetcher/.venv/bin/python fbs_shipment.py --dry-run -v --out /tmp/fbs
/opt/openclaw/skills/ym_stocks_fetcher/.venv/bin/python fbs_shipment.py --out /tmp/fbs   # + Drive

sudo systemctl restart ym-supplies-bot
```

В Telegram: `/fbs` → «📄 Документы» → придут два PDF и ссылка на папку.

**Первая проверка «Собрано»** — на одном реальном заказе, затем сверить
статус в кабинете. Кнопка меняет статус в Маркете, отменить из бота нельзя.

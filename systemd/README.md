# Systemd обвязка для ym_stocks_fetcher_v2

## Что внутри

```
systemd/
├── install.sh                          # установщик (запускать от root)
├── units/
│   ├── ym-daily.service                # дневной pipeline (один-shot)
│   ├── ym-daily.timer                  # триггер 08:00 ежедневно
│   ├── ym-monthly-history.service      # ежемесячный backfill истории
│   └── ym-monthly-history.timer        # триггер 5-го числа в 06:00
└── scripts/
    ├── ym_run.sh                       # обёртка venv+env+cwd→python
    ├── ym_daily.sh                     # stocks → orders → forecast
    ├── ym_monthly_history.sh           # импорт прошлого месяца
    └── ym_manual.sh                    # ручной запуск шагов
```

## Расписание

| Когда | Что |
|---|---|
| Ежедневно 08:00 | `fetch_ym_stocks` → `fetch_ym_orders` → `forecast_stocks` (с Telegram) |
| 5-го числа 06:00 | `fetch_ym_report --year ПРЕД_ГОД --month ПРЕД_МЕСЯЦ --import-and-download` |

Время — серверное (TZ хост-системы).

## Установка

```bash
cd /tmp
unzip ym_systemd_v1.zip
cd systemd
sudo bash install.sh
```

Установщик:
1. Кладёт скрипты в `/opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/`
2. Кладёт `.service` и `.timer` в `/etc/systemd/system/`
3. `systemctl daemon-reload`
4. `systemctl enable --now ym-daily.timer ym-monthly-history.timer`
5. Прогоняет `forecast --dry-run` как самопроверку

## Управление

```bash
# Запустить pipeline прямо сейчас (не дожидаясь 08:00)
sudo systemctl start ym-daily.service

# Статус последнего запуска (exit-код, длительность, тэйл логов)
systemctl status ym-daily.service

# Когда следующий запуск
systemctl list-timers ym-*.timer

# Логи pipeline за сегодня
journalctl -u ym-daily.service --since today

# Логи в реальном времени
journalctl -u ym-daily.service -f

# Все логи за последнюю неделю
journalctl -u ym-daily.service --since "7 days ago"

# Только сообщения с ошибками
journalctl -u ym-daily.service -p err

# Хранение логов
# journald по умолчанию ротирует логи сам (см. /etc/systemd/journald.conf:
# SystemMaxUse и MaxFileSec). Дополнительная настройка обычно не нужна.
```

## Ручные операции

`ym_manual.sh` — для отладки. Вывод сразу в терминал, не в журнал:

```bash
# Полный pipeline вручную с подробным выводом
sudo /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh all

# Только прогноз, без записи и без Telegram (если убрать TG_BOT_TOKEN из env)
sudo /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh forecast --dry-run -v

# Импорт конкретного месяца истории
sudo /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh history 2026 4

# Что есть ещё
/opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh help
```

## Изменение расписания

Отредактируйте таймер:

```bash
sudo systemctl edit ym-daily.timer
```

Это откроет `override.conf`. Например, для запуска **два раза в день**:

```ini
[Timer]
# Сначала очищаем дефолтный OnCalendar (важно!)
OnCalendar=
# Потом задаём новый — каждый день в 08:00 и 18:00
OnCalendar=*-*-* 08:00:00
OnCalendar=*-*-* 18:00:00
```

После сохранения:
```bash
sudo systemctl daemon-reload
sudo systemctl restart ym-daily.timer
systemctl list-timers ym-daily.timer    # проверить новое время
```

Если редактировать через `edit` неудобно — можно прямо в файле
`/etc/systemd/system/ym-daily.timer`, после чего так же `daemon-reload`.

## Отключение / удаление

```bash
# Временно остановить
sudo systemctl stop ym-daily.timer ym-monthly-history.timer

# Отключить от автозапуска (но оставить юниты для возврата)
sudo systemctl disable ym-daily.timer ym-monthly-history.timer

# Полное удаление
sudo systemctl disable --now ym-daily.timer ym-monthly-history.timer
sudo rm /etc/systemd/system/ym-daily.{service,timer}
sudo rm /etc/systemd/system/ym-monthly-history.{service,timer}
sudo rm -rf /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts
sudo systemctl daemon-reload
```

## Особенности конфигурации

- **`Persistent=true`** в таймере: если сервер был выключен в 08:00 —
  pipeline запустится автоматически при ближайшем включении. Это лучше
  чем cron, где пропуск означает пропуск.
- **`RandomizedDelaySec=300`** в daily: разброс ±5 минут чтобы не
  долбить YM API в одну и ту же секунду каждое утро (хорошие манеры).
- **`RuntimeMaxSec=600`** в daily service: если что-то зависло
  (например, gspread не отвечает), systemd убьёт через 10 минут.
- **`OnFailure=`** не настроен. Если нужны алерты при падении — могу
  добавить отдельный сервис, который отправит в Telegram сообщение
  "pipeline failed".
- **Логи в journald**, не в файлы. Journald сам ротирует. Если нужны
  отдельные файлы — настройте `ForwardToSyslog=yes` в journald.conf
  или добавьте `StandardOutput=append:/var/log/ym-daily.log` в service.

## Telegram

Сейчас Telegram отправляется на **каждом запуске** (вы это и просили).
Если в будущем захотите отдельный режим «без алерта», самый чистый путь —
unset переменных перед вызовом forecast:

```ini
# В override.conf для ym-daily.service:
[Service]
Environment="TG_BOT_TOKEN="
Environment="TG_ADMIN_CHAT_ID="
```

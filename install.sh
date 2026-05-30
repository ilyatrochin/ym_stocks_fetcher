#!/usr/bin/env bash
# ============================================================================
# install.sh — установка systemd timers для ym_stocks_fetcher.
#
# Запуск (от root):
#   sudo bash install.sh
# ============================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: requires root (use: sudo bash install.sh)" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_SCRIPTS="/opt/openclaw/skills/ym_stocks_fetcher_patched/scripts"
DEST_UNITS="/etc/systemd/system"

echo "▶ Installing ym_stocks_fetcher systemd timers..."
echo

# ── 1. Скрипты ────────────────────────────────────────────────────────
echo "→ Installing scripts to $DEST_SCRIPTS"
mkdir -p "$DEST_SCRIPTS"
install -m 0755 "$SRC_DIR/scripts/ym_run.sh"             "$DEST_SCRIPTS/"
install -m 0755 "$SRC_DIR/scripts/ym_daily.sh"           "$DEST_SCRIPTS/"
install -m 0755 "$SRC_DIR/scripts/ym_monthly_history.sh" "$DEST_SCRIPTS/"
install -m 0755 "$SRC_DIR/scripts/ym_manual.sh"          "$DEST_SCRIPTS/"
echo "  ✅ Done"

# ── 2. systemd units ──────────────────────────────────────────────────
echo "→ Installing systemd units to $DEST_UNITS"
install -m 0644 "$SRC_DIR/units/ym-daily.service"            "$DEST_UNITS/"
install -m 0644 "$SRC_DIR/units/ym-daily.timer"              "$DEST_UNITS/"
install -m 0644 "$SRC_DIR/units/ym-monthly-history.service"  "$DEST_UNITS/"
install -m 0644 "$SRC_DIR/units/ym-monthly-history.timer"    "$DEST_UNITS/"
echo "  ✅ Done"

# ── 3. systemd reload + enable ────────────────────────────────────────
echo "→ Reloading systemd daemon"
systemctl daemon-reload

echo "→ Enabling timers"
systemctl enable ym-daily.timer
systemctl enable ym-monthly-history.timer

echo "→ Starting timers"
systemctl start ym-daily.timer
systemctl start ym-monthly-history.timer

# ── 4. Smoke-test (через сам сервис, dry-run-эффект через ручной запуск) ──
echo
echo "→ Smoke-test: запуск forecast через ym_manual.sh --dry-run"
echo "  (это проверит venv, secrets, Sheets — БЕЗ записи и БЕЗ Telegram)"
echo

if "$DEST_SCRIPTS/ym_manual.sh" forecast --dry-run 2>&1 | tail -15; then
    echo
    echo "  ✅ Smoke-test пройден"
else
    echo
    echo "  ⚠ Smoke-test упал — таймеры всё равно поставлены, но первый"
    echo "    запуск может фейлиться. Проверьте окружение."
fi

# ── 5. Сводка ─────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo "✅ Установка завершена."
echo

echo "Расписание:"
systemctl list-timers ym-*.timer --no-pager 2>/dev/null \
    || echo "  (нет timers — что-то пошло не так)"

echo
echo "Полезные команды:"
cat << 'EOF'
  # Запустить pipeline вручную прямо сейчас:
  systemctl start ym-daily.service

  # Посмотреть статус последнего запуска:
  systemctl status ym-daily.service

  # Логи pipeline в реальном времени:
  journalctl -u ym-daily.service -f

  # Все логи за сегодня:
  journalctl -u ym-daily.service --since today

  # Только сегодняшний последний прогон:
  journalctl -u ym-daily.service -n 200 --no-pager

  # Когда следующий запуск:
  systemctl list-timers ym-*.timer

  # Импорт конкретного месяца истории (вручную):
  /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh history 2026 4

  # Запустить monthly backfill за прошлый месяц прямо сейчас:
  systemctl start ym-monthly-history.service

  # Отладочный ручной запуск с выводом в терминал:
  /opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_manual.sh forecast --dry-run -v
EOF
echo "════════════════════════════════════════════════════════════════"

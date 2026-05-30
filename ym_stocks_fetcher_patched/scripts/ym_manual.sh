#!/usr/bin/env bash
# ============================================================================
# ym_manual.sh — ручной запуск шагов для отладки. Вывод в терминал.
#
# Примеры:
#   ym_manual.sh stocks                  # только остатки
#   ym_manual.sh orders                  # только заказы
#   ym_manual.sh supplies                # только товары в пути
#   ym_manual.sh supplies --dry-run -v   # supplies без записи в Sheets
#   ym_manual.sh forecast                # прогноз + Telegram
#   ym_manual.sh forecast --dry-run      # прогноз без записи и без Telegram
#   ym_manual.sh history 2026 5          # импорт мая 2026 в sales_history
#   ym_manual.sh all                     # полный pipeline (4 шага)
#
# Для постоянных запусков пользуйтесь systemctl start ym-daily.service —
# это даёт более чистый интерфейс через journalctl.
# ============================================================================
set -uo pipefail

VENV_DIR="/opt/openclaw/skills/ym_stocks_fetcher/.venv"
SECRETS_FILE="/opt/openclaw/secrets/.env"
MODULE_DIR="/opt/openclaw/skills/ym_stocks_fetcher_patched/ym_stocks_fetcher_v2"

run() {
    local script="$1"; shift
    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "▶ $script $*"
    echo "════════════════════════════════════════════════════════════════"
    source "$VENV_DIR/bin/activate"
    set -a; source "$SECRETS_FILE"; set +a
    cd "$MODULE_DIR"
    python "$script" "$@"
}

case "${1:-help}" in
    stocks)   run fetch_ym_stocks.py "${@:2}" ;;
    orders)   run fetch_ym_orders.py "${@:2}" ;;
    supplies) run fetch_ym_supplies.py "${@:2}" ;;
    forecast) run forecast_stocks.py "${@:2}" ;;
    history)
        YEAR="${2:?Usage: ym_manual.sh history YEAR MONTH}"
        MONTH="${3:?Usage: ym_manual.sh history YEAR MONTH}"
        run fetch_ym_report.py --year "$YEAR" --month "$MONTH" \
            --import-and-download -v
        ;;
    all)
        run fetch_ym_stocks.py
        run fetch_ym_orders.py
        run fetch_ym_supplies.py
        run forecast_stocks.py
        ;;
    *)
        cat << EOF
Usage: $(basename "$0") <command> [args]

Commands:
  stocks               — fetch_ym_stocks.py
  orders               — fetch_ym_orders.py
  supplies             — fetch_ym_supplies.py (товары в пути на склад МП)
  forecast [args]      — forecast_stocks.py
  history YEAR MONTH   — fetch_ym_report.py (импорт в sales_history)
  all                  — все четыре ежедневных шага по очереди

Для постоянных запусков лучше использовать systemd:
  systemctl start ym-daily.service           # запустить pipeline сейчас
  systemctl status ym-daily.service          # статус последнего запуска
  systemctl list-timers ym-*                 # ближайшие срабатывания
  journalctl -u ym-daily.service -f          # логи в реальном времени
  journalctl -u ym-daily.service --since today
EOF
        ;;
esac

#!/usr/bin/env bash
# ============================================================================
# ym_daily.sh — ежедневный pipeline: stocks → orders → forecast
#
# Выполняется через systemd service ym-daily.service
# Логи: journalctl -u ym-daily.service
#
# Если step падает — следующие не выполняются (set -e + явные проверки),
# systemd зафиксирует FAILED и сможет триггернуть OnFailure-уведомление.
# ============================================================================
set -uo pipefail

WRAPPER="/opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_run.sh"

step() {
    local name="$1"; shift
    echo
    echo "▶ STEP: $name"
    if "$WRAPPER" "$@"; then
        echo "✅ $name OK"
    else
        local rc=$?
        echo "❌ $name FAILED (exit=$rc)"
        exit "$rc"
    fi
}

echo "▶ Daily pipeline START at $(date +'%Y-%m-%d %H:%M:%S')"

step "1/4 fetch_ym_stocks"    fetch_ym_stocks.py
step "2/4 fetch_ym_orders"    fetch_ym_orders.py
step "3/4 fetch_ym_supplies"  fetch_ym_supplies.py
step "4/4 forecast_stocks"    forecast_stocks.py

echo
echo "✅ Daily pipeline DONE at $(date +'%Y-%m-%d %H:%M:%S')"

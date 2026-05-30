#!/usr/bin/env bash
# ============================================================================
# ym_monthly_history.sh — импорт прошлого завершившегося месяца через Reports API.
#
# Выполняется через ym-monthly-history.service
# ============================================================================
set -uo pipefail

WRAPPER="/opt/openclaw/skills/ym_stocks_fetcher_patched/scripts/ym_run.sh"

# Последний день прошлого месяца
PREV_MONTH_END=$(date -d "$(date +%Y-%m-01) -1 day" +%Y-%m-%d)
YEAR=$(date -d "$PREV_MONTH_END" +%Y)
MONTH=$(date -d "$PREV_MONTH_END" +%-m)   # без ведущего нуля

echo "▶ Monthly history backfill: year=$YEAR month=$MONTH"

"$WRAPPER" fetch_ym_report.py --year "$YEAR" --month "$MONTH" \
    --import-and-download

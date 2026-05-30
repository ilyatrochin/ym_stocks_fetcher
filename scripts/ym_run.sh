#!/usr/bin/env bash
# ============================================================================
# ym_run.sh — обёртка для запуска python-скрипта из ym_stocks_fetcher_v2
#
# Под systemd: вывод идёт в stdout/stderr, journald его подхватит.
# Никаких самописных лог-файлов — всё через journalctl.
#
# Использование:
#   ym_run.sh <script.py> [args...]
# ============================================================================
set -euo pipefail

VENV_DIR="/opt/openclaw/skills/ym_stocks_fetcher/.venv"
SECRETS_FILE="/opt/openclaw/secrets/.env"
MODULE_DIR="/opt/openclaw/skills/ym_stocks_fetcher_patched/ym_stocks_fetcher_v2"

[[ -d "$VENV_DIR" ]] || { echo "ERROR: venv not found: $VENV_DIR" >&2; exit 2; }
[[ -f "$SECRETS_FILE" ]] || { echo "ERROR: secrets not found: $SECRETS_FILE" >&2; exit 2; }
[[ -d "$MODULE_DIR" ]] || { echo "ERROR: module not found: $MODULE_DIR" >&2; exit 2; }
[[ $# -ge 1 ]] || { echo "Usage: $0 <script.py> [args...]" >&2; exit 2; }

SCRIPT="$1"
shift
[[ -f "$MODULE_DIR/$SCRIPT" ]] || {
    echo "ERROR: script not found: $MODULE_DIR/$SCRIPT" >&2
    exit 2
}

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a

cd "$MODULE_DIR"

echo "════════════════════════════════════════════════════════════════"
echo "[$(date +'%Y-%m-%d %H:%M:%S')] START $SCRIPT $*"
echo "  pid=$$  cwd=$(pwd)"
echo "════════════════════════════════════════════════════════════════"

exec python "$SCRIPT" "$@"

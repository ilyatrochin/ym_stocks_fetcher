#!/usr/bin/env bash
# ============================================================================
# deploy.sh — обновить установку из git до указанной версии.
#
# Вызывается из GitHub Actions, либо руками:
#   bash scripts/deploy.sh                  # текущая ветка → main
#   bash scripts/deploy.sh v0.4.0           # конкретный тег
#   bash scripts/deploy.sh develop          # ветка develop (для теста)
#
# Запускается от пользователя openclaw (НЕ от root).
# ============================================================================
set -euo pipefail

REPO_DIR="/opt/openclaw/skills/ym_stocks_fetcher_patched"
VENV_DIR="/opt/openclaw/skills/ym_stocks_fetcher/.venv"
TARGET="${1:-main}"

cd "$REPO_DIR"

echo "▶ Текущий HEAD до деплоя:"
git log -1 --oneline

echo "▶ git fetch"
git fetch --tags --prune origin

# Запоминаем что было — для diff и для логирования
OLD_SHA=$(git rev-parse HEAD)

echo "▶ git checkout $TARGET"
git checkout "$TARGET"

# Если TARGET — это ветка (не тег), подтянуть свежий снапшот
if git symbolic-ref -q HEAD >/dev/null; then
    echo "▶ git pull (это ветка)"
    git pull --ff-only origin "$TARGET"
fi

NEW_SHA=$(git rev-parse HEAD)
echo "▶ Новый HEAD: $(git log -1 --oneline)"

# Если ничего не изменилось — можно не дёргать venv/install
if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
    echo "✅ Без изменений, нечего обновлять"
    exit 0
fi

# Если изменились зависимости — обновить venv
if git diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -q 'requirements.txt'; then
    echo "▶ requirements.txt изменился — обновляю venv"
    "$VENV_DIR/bin/pip" install -r ym_stocks_fetcher_v2/requirements.txt --quiet
else
    echo "▶ requirements.txt без изменений, venv не трогаем"
fi

# Если изменились systemd-units — нужен sudo, скрипт сам сообщит
if git diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -qE '^systemd/units/|^systemd/install\.sh'; then
    echo "⚠ Изменились systemd unit-файлы — нужно вручную:"
    echo "    sudo bash $REPO_DIR/systemd/install.sh"
    echo "  Из-под Actions это сделать нельзя без sudo."
fi

# Чистим __pycache__ — старые .pyc могут перекрывать новый код
find "$REPO_DIR/ym_stocks_fetcher_v2" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "▶ Deploy diff:"
git --no-pager diff --stat "$OLD_SHA" "$NEW_SHA"

echo "✅ Deploy готов: $OLD_SHA → $NEW_SHA"
#!/usr/bin/env bash
# ============================================================================
# init-git.sh — первичная инициализация репозитория НА ЛОКАЛЬНОЙ МАШИНЕ.
#
# НЕ запускать на сервере! Для сервера см. MIGRATION-TO-GIT.md.
#
# Использование:
#   ./init-git.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# ── 1. Проверки ──────────────────────────────────────────────────────
if [[ -d .git ]]; then
    echo "❌ .git уже существует. Этот скрипт только для первичной инициализации." >&2
    exit 1
fi
[[ -f .gitignore ]] || { echo "❌ нет .gitignore — отказываюсь работать"; exit 1; }

# ── 2. Init + первая ветка ───────────────────────────────────────────
echo "▶ git init"
git init -b main

# ── 3. Защита от случайного коммита секретов ─────────────────────────
DANGER_PATTERNS=(.env .env.local gcp-sa.json service-account.json)
for f in "${DANGER_PATTERNS[@]}"; do
    if [[ -f "$f" ]]; then
        if git check-ignore -q "$f"; then
            echo "✓ $f игнорируется (ок)"
        else
            echo "❌ $f НЕ игнорируется .gitignore. Останавливаюсь." >&2
            exit 1
        fi
    fi
done

# ── 4. Стейджинг и проверка состава ──────────────────────────────────
echo "▶ git add ."
git add .

echo
echo "▶ Что попадёт в первый коммит:"
git status --short | head -50
TOTAL=$(git diff --cached --name-only | wc -l)
echo "  всего файлов: $TOTAL"

# Финальная проверка — не закралось ли что-то опасное
echo
echo "▶ Финальная проверка на секреты в индексе:"
DANGER_REGEX='(\.env$|\.env\.|gcp-sa\.json|service-account|credentials\.json|\.pem$|\.key$)'
if git diff --cached --name-only | grep -E "$DANGER_REGEX" | grep -v '\.example$'; then
    echo "❌ В индекс попали потенциальные секреты! Останавливаюсь." >&2
    exit 1
fi
echo "  ✓ Чисто"

# ── 5. Первый коммит ─────────────────────────────────────────────────
echo
echo "▶ Делаю initial commit"
git commit -m "Initial commit: ym_stocks_fetcher (модули 1+2+3, Patch 2)

- Модуль 1: остатки + оборачиваемость (fetch_ym_stocks.py)
- Модуль 2: заказы → продажи (fetch_ym_orders.py)
- Модуль 3: товары в пути (fetch_ym_supplies.py)
- Прогноз с учётом транзита (forecast_stocks.py)
- Patch 1: inf-санитизация и batch-удаление (sheets_helpers.py)
- Patch 2: qty_full=FIT, дедуп блока 'В пути', whitelist warehouse_routing
- systemd-обвязка: timers + service-скрипты"

# Метка — соответствует Patch 2.3
git tag -a v0.3.0 -m "v0.3.0: модуль 3 (supplies) + Patch 2 (FIT, дедуп, routing whitelist)"

# ── 6. Что дальше ────────────────────────────────────────────────────
cat <<'EOF'

✅ Готово.

Дальше — привязать к GitHub (создай ПРИВАТНЫЙ репозиторий, без README):

  git remote add origin git@github.com:<USER>/ym_stocks_fetcher.git
  git push -u origin main
  git push --tags

Заведи ветку develop как площадку для будущих фич:

  git checkout -b develop
  git push -u origin develop

Дальше для деплоя на сервер — см. MIGRATION-TO-GIT.md.

EOF

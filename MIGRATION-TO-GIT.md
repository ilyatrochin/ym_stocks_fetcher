# Миграция работающего сервера в git

Цель: превратить существующую установку
`/opt/openclaw/skills/ym_stocks_fetcher_patched/` в git-клон **без**
переустановки systemd, venv, секретов. После этого деплой апдейтов
становится `git pull`.

**Что НЕ ломается:**
- venv `/opt/openclaw/skills/ym_stocks_fetcher/.venv` — остаётся как есть.
- Секреты `/opt/openclaw/secrets/.env` и `gcp-sa.json` — не трогаем.
- systemd units в `/etc/systemd/system/ym-*` — не трогаем.
- Логи journald — не теряются.
- Расписание (08:00 ежедневно) — не меняется.

---

## Часть 1. На локальной машине (один раз)

### 1.1. Подготовка репозитория

Скачай этот архив, распакуй в папку. Структура внутри уже правильная:

```
ym_stocks_fetcher/
├── .gitignore
├── README.md
├── CLAUDE.md, CHANGES.md, SKILL.md, DEPLOY.md, LICENSE
├── init-git.sh
├── MIGRATION-TO-GIT.md          ← этот файл
├── examples/.env.example
├── systemd/
│   ├── install.sh
│   └── units/                   (4 .service/.timer файла)
└── ym_stocks_fetcher_patched/
    ├── scripts/                 (ym_run, ym_daily, ym_manual, ym_monthly_history)
    └── ym_stocks_fetcher_v2/    (Python-модули)
```

**Важно:** в репо нет `fetch_ym_report.py`. Этот файл существует на сервере
(используется `ym_monthly_history.sh`), но в моих исходниках его нет —
он появился до начала наших правок. Перед коммитом:

```bash
# Скопировать с сервера
scp user@server:/opt/openclaw/skills/ym_stocks_fetcher_patched/ym_stocks_fetcher_v2/fetch_ym_report.py \
    ym_stocks_fetcher/ym_stocks_fetcher_patched/ym_stocks_fetcher_v2/
```

### 1.2. Создаём приватный репозиторий на GitHub

В web-интерфейсе:
- New repository → `ym_stocks_fetcher`
- ✅ **Private**
- НЕ инициализируй с README/license/.gitignore — у нас уже есть свои.

### 1.3. Первый коммит и push

```bash
cd ym_stocks_fetcher
chmod +x init-git.sh
./init-git.sh
```

`init-git.sh` сам проверит что `.env` и `gcp-sa.json` (если они вдруг рядом)
не попадают в коммит. Если попали — остановится.

Затем:

```bash
git remote add origin git@github.com:<USER>/ym_stocks_fetcher.git
git push -u origin main
git push --tags
git checkout -b develop
git push -u origin develop
```

---

## Часть 2. На сервере (один раз)

### 2.1. SSH-ключ для git

GitHub нужно научить пускать сервер. Самый чистый способ — **Deploy Key**:
ключ привязан к конкретному репо, никаких прав на остальные.

На сервере:

```bash
# Генерируем ключ для пользователя, от которого работают systemd-сервисы.
# Сейчас это root (User=root в ym-daily.service), но git-операции
# желательно делать НЕ от рута. Возьмём пользователя openclaw если он есть,
# или создадим:
id openclaw &>/dev/null || sudo useradd -r -s /bin/bash -m openclaw

sudo -u openclaw bash -c '
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    [ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -C "ym-fetcher@$(hostname)" \
        -f ~/.ssh/id_ed25519 -N ""
    cat ~/.ssh/id_ed25519.pub
'
```

Скопируй вывод (это публичный ключ) и добавь в GitHub:

> Repo → Settings → Deploy keys → Add deploy key
> Title: `production server`
> Key: (paste)
> ✅ Allow write access — **НЕ ставим**. Сервер только читает.

Проверка соединения:

```bash
sudo -u openclaw ssh -T git@github.com
# должно ответить: Hi <username>/ym_stocks_fetcher! You've successfully authenticated...
```

### 2.2. Превращаем существующую установку в git-клон

Текущая папка `/opt/openclaw/skills/ym_stocks_fetcher_patched/` содержит
ровно те же файлы, что в репозитории (с точностью до правок Patch 2).
Мы не клонируем поверх (потеряются права/файлы), а **инициализируем
git внутри уже существующей папки**.

```bash
# Шаг А. Бэкап на всякий случай
sudo cp -ra /opt/openclaw/skills/ym_stocks_fetcher_patched \
            /opt/openclaw/skills/.pre-git-$(date +%Y%m%d-%H%M%S)

cd /opt/openclaw/skills/ym_stocks_fetcher_patched

# Шаг Б. Чиним владельца — git должен работать от openclaw, не от root
sudo chown -R openclaw:openclaw .

# Шаг В. Init + remote
sudo -u openclaw bash <<'EOF'
    git init -b main
    git remote add origin git@github.com:<USER>/ym_stocks_fetcher.git
    git fetch origin
EOF
```

Теперь самый деликатный момент. У нас в локальной папке есть файлы, и в
remote есть файлы. Эти файлы должны быть **одинаковыми**, но git об этом
не знает. Делаем «soft reset на remote, оставив рабочую копию как есть»:

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher_patched

sudo -u openclaw bash <<'EOF'
    # Создаём .gitignore локально, чтобы git не предложил коммитить мусор
    cat > .gitignore <<'GITIGNORE'
.env
.env.*
!.env.example
gcp-sa.json
__pycache__/
*.pyc
.venv/
venv/
*.log
.backup-*/
.pre-git-*/
GITIGNORE

    # Подтянуть индекс remote-ветки на main, не трогая рабочую копию
    git reset --soft origin/main
    
    # Посмотреть что git думает о расхождениях
    git status
EOF
```

Здесь нужно остановиться и **посмотреть глазами**, что показывает `git status`.

**Идеальный случай:** «nothing to commit, working tree clean». Это значит,
что файлы на сервере побитово совпадают с remote — миграция готова, см. 2.3.

**Реалистичный случай:** git покажет десяток «modified» — это файлы,
где на сервере что-то старее (из-за патчей и ручных правок) или новее
(тот же `fetch_ym_report.py`, если он есть только на сервере и не попал
в коммит).

```bash
# Посмотреть разницу по конкретному файлу:
sudo -u openclaw git diff -- path/to/file
```

**Что делать:**

- Если файл на сервере **старее, чем в репо** (например, ты ещё не
  накатил Patch 2): возьми версию из repo:
  ```bash
  sudo -u openclaw git checkout -- path/to/file
  ```
  Это тот же эффект, что прислали тебе апдейт и ты его установил.
  
- Если файл на сервере **новее, чем в репо** (например, локальный
  hotfix, который не успел попасть в git): сначала зафиксируй его
  локально в репо (на твоей машине, commit + push), потом на сервере
  `git pull`.

- Если файл вообще не в репо (`fetch_ym_report.py`?): остался в Untracked.
  Закоммить его в репо локально по аналогии с пунктом выше.

**Когда `git status` показывает clean** — миграция готова.

### 2.3. Привязываем ветку

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher_patched

sudo -u openclaw bash <<'EOF'
    git branch --set-upstream-to=origin/main main
    git status
    git log --oneline -5
EOF
```

### 2.4. Smoke-тест

```bash
# Запустить pipeline через systemd прямо сейчас — должен отработать как обычно
sudo systemctl start ym-daily.service
journalctl -u ym-daily.service -f
# Ctrl+C когда увидишь "Daily pipeline DONE"
```

Если pipeline отработал — миграция готова.

### 2.5. (Опционально) Удалить бэкап через неделю стабильной работы

```bash
sudo rm -rf /opt/openclaw/skills/.pre-git-*
```

---

## Часть 3. Как теперь обновлять сервер

### 3.1. Обычный апдейт (новый Patch / релиз)

На локальной машине:

```bash
# Делаешь правки, тестируешь локально
git checkout -b feature/something
# (правишь файлы)
git add . && git commit -m "Описание"
git push -u origin feature/something
# Сделай Pull Request develop ← feature/something (или сразу merge)
git checkout develop && git merge feature/something && git push
# Когда готов в прод:
git checkout main && git merge develop
git tag -a v0.4.0 -m "v0.4.0: что добавлено"
git push origin main --tags
```

На сервере:

```bash
ssh user@server
cd /opt/openclaw/skills/ym_stocks_fetcher_patched
sudo -u openclaw git fetch
sudo -u openclaw git checkout v0.4.0      # или main, или develop для теста
# Если изменились зависимости — обновить venv:
sudo /opt/openclaw/skills/ym_stocks_fetcher/.venv/bin/pip install -r ym_stocks_fetcher_v2/requirements.txt
# Если изменились systemd-units — переустановить:
sudo bash systemd/install.sh
# Smoke-тест:
sudo systemctl start ym-daily.service
journalctl -u ym-daily.service -f
```

### 3.2. Откат на прошлую версию

Любая версия — это тег.

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher_patched
sudo -u openclaw git checkout v0.3.0
# Если systemd-units откатываются:
sudo bash systemd/install.sh
```

### 3.3. Проверка что на сервере именно та версия что ожидается

```bash
cd /opt/openclaw/skills/ym_stocks_fetcher_patched
git describe --tags        # покажет v0.3.0 или v0.3.0-2-gabc1234
git log -1 --oneline
git status                 # должно быть "clean"
```

Если показывает «modified» — значит кто-то правил файлы на сервере
руками в обход git. Это плохо, но поправимо: либо откати правки
(`git checkout -- path`), либо вынеси их в коммит локально и подтяни.

---

## Часть 4. Что важно помнить

### 4.1. Никогда не редактируй файлы прямо на сервере

Иначе при следующем `git pull` будет конфликт. Если очень нужно
быстро править в проде — после правки:

```bash
sudo -u openclaw git diff > /tmp/hotfix.diff
# Скопировать /tmp/hotfix.diff к себе, применить локально, закоммитить,
# запушить, на сервере git reset --hard origin/main
```

### 4.2. Секреты НИКОГДА не в репо

`.gitignore` страхует на уровне ребяти, но привычка важнее.
Если запушил по ошибке — **немедленно отозвать ключи** в кабинете
Маркета, GCP, BotFather. История git публикуется в GitHub mirror
почти мгновенно.

### 4.3. Деплой ключ — только read-only

В Deploy Keys на GitHub не давай Allow write access. Тогда даже
если сервер скомпрометируют — атакующий не сможет запушить
вредоносный коммит в репо.

### 4.4. Бэкапы

`/opt/openclaw/skills/.pre-git-*` — это бэкап от миграции, его можно
удалить через неделю стабильной работы. `.backup-*` от старого
`update.sh` (если ещё лежат) — можно удалить сразу, теперь у нас
git как механизм отката.

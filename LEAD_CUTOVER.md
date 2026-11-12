# Переключение заявок на PostgreSQL на сервере

Эта инструкция для текущего сервера с backend в `host`-сети и отдельной базой
`chatbot-storage` на `127.0.0.1:5432`. Предполагается, что миграции, обе клиники,
права роли, восстановление дампа и вход через localhost уже проверены.

## До окна

Выполняйте команды в одной сессии Bash на сервере из корня репозитория.
Сначала убедитесь, что подтянут коммит с помощником. Функции используют уже
созданные закрытые файлы в `/root`.

```bash
cd /opt/chatbot

files() {
  docker compose -f docker-compose.yml "$@"
}

sql() {
  env -u LEAD_DB_PASSWORD docker compose \
    --env-file .env --env-file /root/chatbot-leads.env \
    -f docker-compose.yml -f docker-compose.lead-storage-host.yml "$@"
}

storage() {
  env -u POSTGRES_PASSWORD docker compose --env-file /root/chatbot-storage.env \
    -p chatbot-storage -f docker-compose.storage.yml \
    -f /root/chatbot-storage-loopback.yml "$@"
}

lead_tools() (
  set +x
  LEAD_PGPASSWORD=$(sed -n 's/^LEAD_DB_PASSWORD=//p' /root/chatbot-leads.env)
  [[ "$LEAD_PGPASSWORD" =~ ^[0-9a-f]{64}$ ]] || exit 1
  export LEAD_PGPASSWORD
  storage run --rm -T --no-deps \
    -e LEAD_DATABASE_URL= -e LEAD_PGPASSWORD \
    -e LEAD_PGHOST=postgres -e LEAD_PGPORT=5432 \
    -e LEAD_PGUSER=chatbot_lead_runtime -e LEAD_PGDATABASE=chatbot_storage \
    "$@"
)
```

Подготовьте оба образа заранее. Сборка SQL-образа использует отдельное имя,
поэтому текущий файловый образ остаётся доступен для быстрого возврата.

```bash
current_backend=$(files ps -q backend) &&
test -n "$current_backend" &&
test "$current_backend" = "$(sql ps -q backend)" &&
storage build db-tools &&
sql build backend &&
sql run --rm -T --no-deps --user 1000:1000 --entrypoint python backend scripts/check_lead_startup.py &&
python3 backend/scripts/lead_cutover.py status
```

Одноразовый контейнер проверяет полный запуск и завершение приложения с SQL,
не поднимая HTTP-порт и не отправляя сообщения. Он читает текущие клиентские базы
и восстанавливает сессии из временной копии, если на сервере есть снимок.
При ошибке SQL-инициализации ищите в логе `lead_storage_startup_failed`.
Эта строка содержит этап, класс ошибки и SQLSTATE, если он доступен.

Ожидаем `storage: files`, оба файловых счётчика ожидания по нулям. Посмотрите
на `dead`, если такой статус есть. Проверьте, что в Telegram нет диалога,
который администратор сейчас ведёт. Подготовьте свежий дамп PostgreSQL через
`storage exec -T postgres pg_dump -U storage_owner -d chatbot_storage -Fc`,
сохранив stdout в закрытый файл. После окна понадобится бэкап уже с новыми данными.

## В окно переключения

**1. Остановите backend, затем снимите финальные копии.** Остановка временно
прерывает работу чата. Помощник проверит, что остановился именно файловый backend,
пути и bind mount совпадают, ожидающих отправок нет. Он вернёт путь к новой
папке и признак наличия архива. Сохраните этот путь для возможного отката.

```bash
files stop backend &&
python3 backend/scripts/lead_cutover.py snapshot
```

При ошибке верните чат командой `files up -d --no-build backend` и разберитесь с
причиной до нового окна.

**2. Сверьте снимок, затем импортируйте.** Укажите путь из предыдущего вывода.

```bash
SNAPSHOT=/root/chatbot-cutover.XXXXXXXX
archive=()
if [ -f "$SNAPSHOT/leads_archive.jsonl" ]; then
  archive=(--archive /snapshot/leads_archive.jsonl)
fi

lead_tools -v "$SNAPSHOT:/snapshot:ro" db-tools \
  python scripts/import_lead_history.py --leads /snapshot/leads.jsonl "${archive[@]}"
```

Ожидаем ноль ошибок и дублей, количество по клиникам сходится со снимком.
Пробный импорт ничего не записывает. После сверки:

```bash
lead_tools -v "$SNAPSHOT:/snapshot:ro" db-tools \
  python scripts/import_lead_history.py --leads /snapshot/leads.jsonl "${archive[@]}" --apply &&
lead_tools -v "$SNAPSHOT:/snapshot:ro" db-tools \
  python scripts/lead_cutover.py verify --snapshot /snapshot
```

Должно быть `verified: true`. Проверка сравнивает контрольные суммы файлов,
ID и содержимое заявок. Очередь SQL и квитанции запросов должны быть пусты
перед первым запуском. При любой ошибке оставьте SQL-режим выключенным.

**3. Запустите backend в SQL-режиме и проверьте.**

```bash
sql up -d --no-build backend
sql ps backend
python3 backend/scripts/preflight.py --probes --external https://roshbot.ru
```

В preflight проверьте, что источник заявок `postgres`, база доступна для записи,
Telegram и красные линии проходят. Отправьте одну заявку через `rosh_test`.
Проверьте один лид в отчёте, одну карточку в тестовом Telegram и состояние очереди.

## Возврат на файлы

До запуска SQL-backend достаточно `files up -d --no-build backend`.
После запуска сначала остановите `sql stop backend`. Даже если тестовая заявка
ещё не отправлялась, посетители уже могли создать записи.

При живом PostgreSQL сделайте свежий дамп. Выгрузите новые SQL-заявки из того же
финального снимка в отдельную папку. На сервере папка находится под `/root`,
одноразовый контейнер получает доступ через bind mount.

```bash
ROLLBACK_DIR=$(mktemp -d /root/chatbot-rollback.XXXXXX)
python3 -c 'import os,sys; os.chown(sys.argv[1],1000,1000)' "$ROLLBACK_DIR" &&
lead_tools -v "$SNAPSHOT:/snapshot:ro" -v "$ROLLBACK_DIR:/rollback" db-tools \
  python scripts/import_lead_history.py --leads /snapshot/leads.jsonl "${archive[@]}" \
    --rollback-output /rollback/leads-restored.jsonl
```

Сверьте `exported`, `new_by_company` и `sql_outbox`. Пока есть `pending`,
`failed` или `processing`, разберите исход доставки. Записи `dead` также
просмотрите отдельно. Не отправляйте карточку заново, если она уже пришла.
Если PostgreSQL недоступен, сначала восстановите доступ к последним данным.

Когда сверка закончена, сохраните действующий файл отдельно и установите
подготовленный вместо него, сохранив владельца и режим. Backend должен быть
остановлен. Архив остаётся прежним.

```bash
test "$(docker inspect --format '{{.State.Status}}' chat-widget-backend)" = exited &&
cmp -s "$SNAPSHOT/leads.jsonl" backend/logs/leads.jsonl &&
{
  if [ -f "$SNAPSHOT/leads_archive.jsonl" ]; then
    cmp -s "$SNAPSHOT/leads_archive.jsonl" backend/logs/leads_archive.jsonl
  else
    test ! -e backend/logs/leads_archive.jsonl
  fi
} &&
cp -p backend/logs/leads.jsonl "$ROLLBACK_DIR/leads-before-replace.jsonl" &&
temp_leads=$(mktemp backend/logs/.leads-restored.XXXXXX) &&
cat "$ROLLBACK_DIR/leads-restored.jsonl" > "$temp_leads" &&
chown --reference=backend/logs/leads.jsonl "$temp_leads" &&
chmod --reference=backend/logs/leads.jsonl "$temp_leads" &&
mv -T -- "$temp_leads" backend/logs/leads.jsonl &&
files up -d --no-build backend
```

Проверьте preflight, источник `files`, количество по клиникам и тестовую заявку.
Сохраните базу, очередь и дамп для сверки. При повторном переходе на ту же базу
импортёр сверит сохранённые SQL-ID и не создаст дубли. Команда `verify` выше
предназначена для первого переключения с пустой SQL-очередью и без SQL-квитанций.

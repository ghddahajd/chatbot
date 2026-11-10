# Локальная основа PostgreSQL

Этот этап не переключает runtime. Вход, права, переписки, заявки, доставка и аналитика
продолжают работать по прежнему пути. Приложение не импортирует модуль БД при старте.
Основной docker-compose.yml не меняется. SQL-зависимости устанавливаются отдельно.

## Запуск отдельного контура

Команды выполняются локально из корня проекта. Пароль не вводить в командную строку
и не добавлять в Git. В zsh:

```zsh
read -rs 'POSTGRES_PASSWORD?Пароль отдельной локальной БД: '; echo
export POSTGRES_PASSWORD
docker compose -p chatbot-storage -f docker-compose.storage.yml up -d --wait postgres
docker compose -p chatbot-storage -f docker-compose.storage.yml build db-tools
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm db-tools alembic upgrade head
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm db-tools python scripts/db_admin.py check
```

У PostgreSQL нет опубликованного порта. Том storage_data принадлежит этому Compose-проекту.
Для существующего тома нужен прежний пароль: изменение переменной не меняет пароль в БД.
Инструменты подключаются по внутренней сети. Это доступ владельца БД для миграций,
не будущая учётная запись приложения. Перед подключением runtime понадобится отдельная
роль с ограниченными правами. Пользовательские роли и авторизация в этом этапе отсутствуют.

## Регистрация клиентских баз

По умолчанию db-tools видит rosh-client-data/clients только для чтения.

```bash
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm db-tools python scripts/db_admin.py register-clients --dry-run
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm db-tools python scripts/db_admin.py register-clients
```

Записи читаются через KnowledgeBaseResolver. В БД попадают только внутренний UUID,
company_id, название и время создания/обновления. Повторный запуск сохраняет UUID;
название обновляется, отсутствующие в файлах клиенты не удаляются.
Прайсы, домены и настройки остаются в KB. Реестр ещё не используется для выдачи доступа.
Вся регистрация выполняется одной транзакцией.

Для native-инструментов: отдельное окружение с backend/requirements-db.txt и обычными
зависимостями проекта. Передать DATABASE_URL с драйвером postgresql+asyncpg либо
PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE через окружение. Инструменты не подхватывают
DATABASE_URL из .env автоматически. Не печатать DSN в терминал или сообщения.

## Проверки

На отдельной тестовой БД после миграции:

```bash
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm db-tools alembic check
docker compose -p chatbot-storage -f docker-compose.storage.yml run --rm -e DB_INTEGRATION_TEST=1 -e PYTHONPATH=/workspace/backend db-tools python -m unittest discover -s tests -p test_db_foundation.py -v
```

Тесты создают временные клиентские папки и записи с уникальным префиксом, затем удаляют
только эти записи. Обычный pytest backend/tests пропускает этот модуль: PostgreSQL
не становится обязательной зависимостью текущих тестов.

## Резервная копия и восстановление

Копия и её восстановление проверяются отдельно от отката схемы:

```bash
umask 077
docker compose -p chatbot-storage -f docker-compose.storage.yml exec -T postgres pg_dump -U storage_owner -d chatbot_storage -Fc > /private/tmp/chatbot-storage.dump
docker compose -p chatbot-storage -f docker-compose.storage.yml exec -T postgres createdb -U storage_owner chatbot_restore_check
docker compose -p chatbot-storage -f docker-compose.storage.yml exec -T postgres pg_restore --exit-on-error -U storage_owner -d chatbot_restore_check < /private/tmp/chatbot-storage.dump
docker compose -p chatbot-storage -f docker-compose.storage.yml exec -T postgres psql -U storage_owner -d chatbot_restore_check -c 'SELECT count(*) FROM tenants; SELECT version_num FROM alembic_version;'
```

Дамп может содержать клиентские данные: хранить в закрытом месте, не коммитить.
Сверять не только количество, но и соответствие company_id внутренним UUID.
Повторная регистрация после восстановления должна сохранить эти UUID.

`alembic downgrade base` удаляет реестр и его данные — только на одноразовой тестовой БД.
На рабочей БД это не замена резервному копированию. Текущие JSON/JSONL этим этапом
не импортируются и не изменяются.

Для остановки с сохранением тома:

```bash
docker compose -p chatbot-storage -f docker-compose.storage.yml down
unset POSTGRES_PASSWORD
```

На сервер в этом этапе ничего не выкатывается автоматически. Старые миграции из
v2/storage-foundation не смешивать с новой цепочкой: её единственная начальная
ревизия — 0001_client_registry.

## Подготовка хранения лидов и доставок

Текущий runtime сохраняет JSONL. Работа с файлами отделена от решений диалога:

- `app/storage/leads.py`: `FileLeadStore.append` сохраняет готовый payload,
  `read_all` читает активный файл и необязательный архив для аналитики.
- `app/storage/outbox.py`: `FileOutboxStore.append` дописывает состояние доставки,
  `latest` восстанавливает последнее состояние по `delivery_id`.
- `LeadService` по-прежнему строит payload и вызывает доставку после записи лида.
  `DeliveryService` выбирает получателей, отправляет и рассчитывает повторы.

Форматы JSONL, пути, порядок отправок и правила повторов сохраняются. Архивация
и preflight пока работают с текущими файлами. SQL-зависимости для обычного запуска
не нужны; PostgreSQL этим разделением не включается.

Это разделение ответственности, не транзакция: сбой между записью лида и постановкой
доставки остаётся возможным. Следующий этап должен сохранять лид и намерения доставки
одной SQL-транзакцией, затем отправлять их после commit. Нельзя просто заменить два
независимых append на два INSERT и считать сохранение атомарным.

Очередь карточек Telegram-моста остаётся отдельным механизмом. Перенос generic outbox
сам по себе не обеспечивает её надёжность. Служебные URL и исторические данные
также требуют отдельных изменений перед SQL-импортом.

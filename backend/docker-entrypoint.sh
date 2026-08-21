#!/bin/sh
set -e

# Реальные данные клиента (backend/data/clients/*) — в .dockerignore, никогда не запекаются в
# образ. Локально их подставляет volume-mount из docker-compose.yml (./backend/data:/app/data).
# На Render (без bind-mount) их неоткуда взять внутри уже собранного образа — поэтому здесь,
# при СТАРТЕ контейнера (не при сборке), подтягиваем их из отдельного приватного репозитория
# по read-only deploy-key. Если ключ не задан (например, локальный docker-compose, где данные
# и так на месте через volume) — просто пропускаем этот шаг, ничего не ломается.

if [ -n "$CLIENT_DATA_DEPLOY_KEY" ]; then
  echo "docker-entrypoint: CLIENT_DATA_DEPLOY_KEY set, pulling client data..."
  KEY_FILE="$(mktemp)"
  printf '%s\n' "$CLIENT_DATA_DEPLOY_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"

  CLONE_DIR="$(mktemp -d)"
  GIT_SSH_COMMAND="ssh -i $KEY_FILE -o StrictHostKeyChecking=no -o IdentitiesOnly=yes" \
    git clone --depth 1 "${CLIENT_DATA_REPO_URL:-git@github.com:ghddahajd/rosh-client-data.git}" "$CLONE_DIR"

  # clients/<id> -> /app/data/clients/<id> (то, что раньше подставлял volume-mount)
  mkdir -p /app/data/clients
  if [ -d "$CLONE_DIR/clients" ]; then
    for dir in "$CLONE_DIR"/clients/*/; do
      name="$(basename "$dir")"
      rm -rf "/app/data/clients/$name"
      cp -r "$dir" "/app/data/clients/$name"
    done
  fi

  # rag_corpus/* -> /client-input/* (то, что раньше подставлял volume-mount ./client-input)
  if [ -d "$CLONE_DIR/rag_corpus" ]; then
    mkdir -p /client-input
    cp -r "$CLONE_DIR"/rag_corpus/. /client-input/
  fi

  rm -rf "$KEY_FILE" "$CLONE_DIR"
  echo "docker-entrypoint: client data ready."
else
  echo "docker-entrypoint: CLIENT_DATA_DEPLOY_KEY not set, skipping client data pull (local dev?)."
fi

# Non-root от сюда и дальше (аудит §2026-08-22) — всё выше (git clone, mkdir/cp в /app/data)
# нарочно ходит от root, потому что права смонтированных ./backend/data и ./backend/logs
# зависят от хоста (локально — мой UID, на проде — кто там разворачивает докер), заранее
# не угадать. chown каждый раз подстраивается под реальные права конкретного запуска, а не
# захардкожен на билде.
#
# Не su — в этом минимальном образе su от root к обычному юзеру просит пароль (PAM,
# не связано с аргументами, проверено отдельно — su -c путал ИЛИ ругался "Authentication
# failure" в зависимости от порядка аргументов, ни один вариант не завёлся). python3 —
# он уже в образе, os.setuid/setgid — сырой syscall, PAM вообще не участвует.
chown -R appuser:appuser /app/data /app/logs
exec python3 -c '
import os, pwd, sys
user = pwd.getpwnam("appuser")
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])
' "$@"

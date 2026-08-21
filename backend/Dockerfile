FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Не USER тут — entrypoint должен стартовать от root (git clone клиентских данных для прода,
# chown смонтированных томов под реальные права хоста), non-root включается уже внутри
# docker-entrypoint.sh перед запуском самого uvicorn, см. там.
RUN useradd --create-home --uid 1000 --shell /bin/sh appuser

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/data ./data
COPY backend/logs ./logs
COPY widget ./widget
COPY demo ./demo
COPY backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

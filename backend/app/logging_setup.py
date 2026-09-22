"""настройка логирования и безопасная запись событий вида `имя ключ=значение`.

До этого модуля в приложении не было logging.basicConfig: строки уровня INFO молча терялись,
а видно было только WARNING и выше. Теперь INFO включён только для логгера "app" — сознательно не
для корня: библиотека httpx на INFO пишет полный URL запроса, а в URL Telegram лежит токен бота.

Правило для событий: только идентификаторы, счётчики и флаги. Тексты сообщений, ответы, телефоны,
имена и ссылки в лог не пишутся (log_event заменяет такие поля на <blocked>).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

APP_LOGGER_NAME = "app"
LOG_FORMAT = "%(asctime)sZ %(levelname)s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
# httpx/httpcore на INFO печатают URL запроса целиком (в нём токен бота Telegram)
QUIET_LOGGERS = ("httpx", "httpcore")
# поля, которые нельзя писать в события: здесь живут персональные данные и секреты
BLOCKED_FIELDS = frozenset(
    {"message", "text", "phone", "name", "answer", "summary", "query", "email", "token", "url", "target"}
)
MAX_VALUE_LENGTH = 80
# телефоноподобная последовательность: 10 и больше цифр с разделителями
_PHONE_LIKE = re.compile(r"\+?\d(?:[\s().-]{0,2}\d){9,}")


def configure_logging(level: str = "INFO") -> None:
    """включает вывод логов приложения; безопасно вызывать повторно."""

    resolved = getattr(logging, str(level).strip().upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        formatter.converter = time.gmtime  # время в логе всегда UTC (суффикс Z)
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(logging.WARNING)

    logging.getLogger(APP_LOGGER_NAME).setLevel(resolved)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def redact_phones(text: str) -> str:
    """маскирует телефоноподобные последовательности в произвольном тексте (например, в ответе модели)."""

    return _PHONE_LIKE.sub("<phone>", text)


def _clean(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).replace("=", ":")
    text = "_".join(text.split())
    return text[:MAX_VALUE_LENGTH] if text else "-"


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """пишет одну строку `event key=value ...`; поля с персональными данными подменяются на <blocked>."""

    try:
        if not logger.isEnabledFor(level):
            return
        parts = [event]
        for key, value in fields.items():
            parts.append(f"{key}=<blocked>" if key in BLOCKED_FIELDS else f"{key}={_clean(value)}")
        logger.log(level, " ".join(parts))
    except Exception:  # noqa: BLE001 — запись события не имеет права ронять запрос, доставку или старт
        pass

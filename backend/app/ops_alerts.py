"""оповещения нам (не клинике) в Telegram: личка или группа.

Файл самодостаточный — только httpx, без остального приложения: его можно скопировать в любой
другой проект. Формат сообщений общий для всех проектов, чтобы в одном чате было видно, откуда что:
«🔴 РОШ · нейросеть — …». Без токена или чата отправка молча выключена.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 15.0
MAX_TEXT_LENGTH = 3500  # у Telegram предел 4096; запас на эмодзи и название проекта


class OpsAlerts:
    def __init__(self, *, bot_token: str, chat_id: str, project: str, proxy_url: str = "") -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.project = project.strip() or "проект"
        self.proxy_url = proxy_url or None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def call(self, method: str, *, _http_timeout: float = SEND_TIMEOUT_SECONDS, **params: Any) -> dict[str, Any] | None:
        """любой метод Bot API; None — не вышло (сеть, отказ Telegram). Исключений не бросает.
        _http_timeout с подчёркиванием: у getUpdates есть свой параметр timeout."""

        if not self.bot_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=_http_timeout, proxy=self.proxy_url) as client:
                response = await client.post(f"https://api.telegram.org/bot{self.bot_token}/{method}", json=params)
            data = response.json()
            if response.status_code == 200 and data.get("ok"):
                return data
            logger.warning("ops_alert call_failed method=%s status=%s", method, response.status_code)
        except Exception as error:  # noqa: BLE001 — оповещение не должно ронять сторожа
            logger.warning("ops_alert call_failed method=%s error=%s", method, type(error).__name__)
        return None

    async def send(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> bool:
        """True — сообщение дошло. Сбой не бросает исключение: сторож повторит в следующий прогон."""

        if not self.enabled:
            return False
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 1] + "…"
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return await self.call("sendMessage", **params) is not None

"""Telegram Topics бридж: long-polling (без вебхука), claim-кнопка внутри темы,
живая пересылка сообщений клиент <-> оператор через отдельную тему на сессию.

Транспорт — getUpdates (long polling), не webhook: не требует публичного HTTPS,
работает из любого окружения (в т.ч. локальный докер без домена). См.
tasks/CODEX_TELEGRAM_TOPICS_BRIDGE_PLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from .models import MessageRole
from .sessions import SessionStore


logger = logging.getLogger(__name__)

CLAIM_CALLBACK_PREFIX = "claim:"
GET_UPDATES_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 40.0
CLOSE_SESSION_COMMANDS = {"/done", "/close", "/end", "/завершить", "/закрыть"}


class TelegramBridgeService:
    """Живой Telegram Topics бридж поверх long polling."""

    def __init__(
        self,
        *,
        bot_token: str,
        group_chat_id: str,
        session_store: SessionStore,
        ws_manager: Any,
    ) -> None:
        self.bot_token = bot_token
        self.group_chat_id = group_chat_id
        self.session_store = session_store
        self.ws_manager = ws_manager
        self._api_base = f"https://api.telegram.org/bot{bot_token}"
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.group_chat_id)

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{self._api_base}/{method}", json=params)
            data = response.json()
            if not data.get("ok"):
                logger.warning(
                    "telegram_bridge api_error method=%s description=%s",
                    method,
                    data.get("description"),
                )
            return data

    async def ensure_topic_for_session(
        self,
        *,
        session_id: str,
        topic_name: str,
        card_text: str,
    ) -> Optional[int]:
        """Создаёт тему для сессии, если её ещё нет. Возвращает topic_id или None."""

        if not self.enabled:
            return None

        session = await self.session_store.get(session_id)
        if session is None:
            return None
        if session.telegram_topic_id is not None:
            return session.telegram_topic_id

        result = await self._call(
            "createForumTopic",
            chat_id=self.group_chat_id,
            name=topic_name[:128],
        )
        if not result.get("ok"):
            return None

        topic_id = result["result"]["message_thread_id"]
        await self.session_store.set_telegram_bridge(session_id, topic_id=topic_id)

        keyboard = {
            "inline_keyboard": [
                [{"text": "Взять в работу", "callback_data": f"{CLAIM_CALLBACK_PREFIX}{session_id}"}]
            ]
        }
        await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=topic_id,
            text=card_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return topic_id

    async def forward_client_message(self, session_id: str, text: str) -> None:
        """Пересылает новое сообщение клиента в уже существующую тему сессии (если есть)."""

        if not self.enabled or not text.strip():
            return
        session = await self.session_store.get(session_id)
        if session is None or session.telegram_topic_id is None:
            return
        await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=session.telegram_topic_id,
            text=f"👤 {text}",
        )

    async def close_topic(self, session_id: str) -> None:
        session = await self.session_store.get(session_id)
        if session is None or session.telegram_topic_id is None or not self.enabled:
            return
        await self._call(
            "closeForumTopic",
            chat_id=self.group_chat_id,
            message_thread_id=session.telegram_topic_id,
        )

    async def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if not data.startswith(CLAIM_CALLBACK_PREFIX):
            return

        session_id = data[len(CLAIM_CALLBACK_PREFIX) :]
        from_user = callback.get("from") or {}
        username = str(from_user.get("username") or from_user.get("first_name") or "оператор")

        session = await self.session_store.get(session_id)
        if session is None or session.telegram_topic_id is None:
            if callback_id:
                await self._call("answerCallbackQuery", callback_query_id=callback_id, text="Сессия не найдена")
            return

        if session.telegram_claimed_by:
            if callback_id:
                await self._call(
                    "answerCallbackQuery",
                    callback_query_id=callback_id,
                    text=f"Уже взято: {session.telegram_claimed_by}",
                )
            return

        await self.session_store.set_telegram_bridge(session_id, claimed_by=username)

        await self._call(
            "editForumTopic",
            chat_id=self.group_chat_id,
            message_thread_id=session.telegram_topic_id,
            name=f"🟢 {username}"[:128],
        )

        message = callback.get("message") or {}
        if message.get("message_id"):
            await self._call(
                "editMessageReplyMarkup",
                chat_id=self.group_chat_id,
                message_id=message["message_id"],
                reply_markup={
                    "inline_keyboard": [[{"text": f"Взято: {username}", "callback_data": "claimed_noop"}]]
                },
            )
        if callback_id:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text="Взяли в работу")

    async def _handle_message(self, message: dict[str, Any]) -> None:
        thread_id = message.get("message_thread_id")
        text = str(message.get("text") or "").strip()
        if not thread_id or not text:
            return

        session = await self.session_store.find_by_telegram_topic(int(thread_id))
        if session is None:
            return

        if text.lower() in CLOSE_SESSION_COMMANDS:
            await self._close_session_from_topic(session.session_id, int(thread_id))
            return

        await self.session_store.append_message(session.session_id, MessageRole.OPERATOR, text)
        payload = {
            "type": "message",
            "role": "operator",
            "text": text,
            "session_id": session.session_id,
        }
        await self.ws_manager.send_to_client(session.session_id, payload)

    async def _close_session_from_topic(self, session_id: str, thread_id: int) -> None:
        """Завершает диалог по команде оператора из темы (/done и т.д.) — переиспользует
        тот же путь закрытия, что и веб-панель оператора (disconnect_operator), плюс
        закрывает саму тему."""

        await self.ws_manager.disconnect_operator(session_id, close_session=True)
        # Сообщение — до закрытия темы: Telegram не даёт постить в уже закрытую тему.
        await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=thread_id,
            text="✅ Диалог завершён.",
        )
        await self.close_topic(session_id)

    async def _process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    async def run_polling_loop(self) -> None:
        if not self.enabled:
            logger.info("telegram_bridge disabled (no bot_token/group_chat_id) — polling not started")
            return

        logger.info("telegram_bridge polling started")
        while True:
            try:
                result = await self._call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=GET_UPDATES_TIMEOUT_SECONDS,
                )
                for update in result.get("result", []):
                    self._offset = int(update["update_id"]) + 1
                    await self._process_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("telegram_bridge polling error=%s", type(error).__name__)
                await asyncio.sleep(5)

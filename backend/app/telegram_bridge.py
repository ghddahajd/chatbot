"""Telegram Topics бридж: long-polling (без вебхука). Три уровня:

- General (группа, без темы) — очередь входящих, которые реально ждут оператора
  (operator_requested / жёсткая мед-эскалация), с кнопкой «Взять в работу».
- Тема "Клиенты" (id темы — telegram_clients_topic_id) — лиды/записи без прямой
  необходимости в операторе (контакт зафиксирован, бот уже дал полный ответ): простая
  карточка, без кнопки, без своей темы — это лог, не живой диалог.
- Тема сессии — создаётся ТОЛЬКО при клейме карточки из General (не заранее, иначе
  плодим темы на заявки, которые никто не забрал), дальше живая пересылка клиент<->оператор.

Транспорт — getUpdates (long polling), не webhook: не требует публичного HTTPS,
работает из любого окружения (в т.ч. локальный докер без домена). См.
tasks/CODEX_TELEGRAM_TOPICS_BRIDGE_PLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from .delivery import _escape_markdown, _iso, _utcnow
from .models import MessageRole, SessionStatus
from .sessions import SessionStore
from .utils.jsonl import append_jsonl, read_jsonl


logger = logging.getLogger(__name__)

CLAIM_CALLBACK_PREFIX = "claim:"
CLOSE_CALLBACK_PREFIX = "close:"
GET_UPDATES_TIMEOUT_SECONDS = 30

# 2026-08-29: заголовок темы читаемый для оператора (#ДДММГГ-порядковый · эмодзи-тип ·
# claimed_by) вместо голого "Сессия <кусок uuid>" — см. TOPIC_TYPE_EMOJI ниже. Имя клиента
# сознательно НЕ включаем — PII в заголовке темы видно даже в превью уведомлений на
# заблокированном телефоне оператора, а само имя никуда не девается — оно всё так же в
# самом лиде/архиве/сессии, просто не торчит в заголовке.
_TOPIC_DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")
TOPIC_TYPE_EMOJI = {
    "complaint": "🔴",
    "self_harm_crisis": "🆘",
    "regulated_advice": "🟡",
}
TOPIC_TYPE_EMOJI_DEFAULT = "🟢"
HTTP_TIMEOUT_SECONDS = 40.0
_MAX_RATE_LIMIT_RETRIES = 3
# Живой баг (ручное тестирование пользователем, 2026-08-26): любая ошибка КРОМЕ 429 (сетевой
# сбой, 409 Conflict от параллельного инстанса, 5xx) раньше не ретраилась вообще — одна
# неудачная попытка теряла карточку насовсем, узнать можно было только руками из
# telegram_bridge_failures.jsonl. Короткий, ограниченный по времени повтор (не фоновая очередь
# как в DeliveryService — тут именно "проверить ещё разок в течение нескольких секунд")
# ловит как раз такие переходные сбои, не отправляя карточку с опозданием в час/день.
_MAX_TRANSIENT_RETRIES = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0


def _telegram_retry_after(data: dict[str, Any]) -> float | None:
    if data.get("error_code") != 429:
        return None
    parameters = data.get("parameters")
    if isinstance(parameters, dict):
        retry_after = parameters.get("retry_after")
        if isinstance(retry_after, (int, float)):
            return float(retry_after)
    return None
CLOSE_SESSION_COMMANDS = {"/done", "/close", "/end", "/завершить", "/закрыть"}
_TELEGRAM_ROLE_LABELS = {"user": "👤 Клиент", "assistant": "🤖 Бот", "operator": "🧑‍💼 Оператор"}
# Название темы должно оставаться осмысленным и в свёрнутом виде сайдбара (Telegram обрезает
# длинные названия справа) — жёсткий потолок на каждую часть, не только на итог, иначе одно
# длинное поле (кривой ник оператора, длинное имя клиента) съедает всё название целиком.
_LABEL_MAX_LENGTH = 18
# Telegram sendMessage режет текст на 4096 символах — бюджет с запасом под остальной текст
# сообщения. Живой баг (2026-08-26): раньше резали по фиксированному количеству сообщений
# (15) — в чате с долгой пляской вокруг оператора (оффер → отказ → повтор) последние 15
# сообщений съедались этой перепиской целиком, а исходные содержательные вопросы клиента в
# начале диалога вытеснялись из сводки. Бюджет по символам вместо количества: короткие
# сообщения не тратят лимит впустую, длинные не вылезают за потолок Telegram.
_TELEGRAM_TRANSCRIPT_CHAR_BUDGET = 3500


def client_label_for_session(session: Any) -> str:
    """Самое информативное, что уже известно про клиента — для имени темы и карточек.
    Имя > телефон > короткий id сессии (последний фолбэк, когда контакт ещё не оставили)."""

    contact_draft = session.contact_draft or {}
    name = str(contact_draft.get("name") or "").strip()
    if name:
        return name[:_LABEL_MAX_LENGTH]
    phone = str(contact_draft.get("phone") or "").strip()
    if phone:
        return phone
    return f"Сессия {session.session_id[:8]}"


def operator_label(from_user: dict[str, Any]) -> str:
    """Короткое имя оператора для названия темы/карточек. first_name почти всегда настоящее
    короткое имя человека — username в Telegram может быть произвольной длинной строкой
    (живой пример: 'sKiTTlesSkiiiiiirRrtEssskeeeetit'), из-за которой название темы обрезалось
    в сайдбаре и терялось целиком. username — только запасной вариант, если first_name пуст."""

    first_name = str(from_user.get("first_name") or "").strip()
    username = str(from_user.get("username") or "").strip()
    label = first_name or username or "оператор"
    return label[:_LABEL_MAX_LENGTH]


def _format_transcript(session: Any) -> str:
    lines: list[str] = []
    total_chars = 0
    for message in reversed(session.messages):
        text = str(getattr(message, "text", "") or "")
        if not text:
            continue
        role_value = getattr(message.role, "value", message.role)
        line = f"{_TELEGRAM_ROLE_LABELS.get(role_value, role_value)}: {text}"
        if lines and total_chars + len(line) + 1 > _TELEGRAM_TRANSCRIPT_CHAR_BUDGET:
            break
        lines.append(line)
        total_chars += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


def _topic_deep_link(group_chat_id: str, topic_id: int) -> str:
    raw_id = group_chat_id.lstrip("-")
    if raw_id.startswith("100"):
        raw_id = raw_id[3:]
    return f"https://t.me/c/{raw_id}/{topic_id}"


class TelegramBridgeService:
    """Живой Telegram Topics бридж поверх long polling."""

    def __init__(
        self,
        *,
        bot_token: str,
        group_chat_id: str,
        session_store: SessionStore,
        ws_manager: Any,
        clients_topic_id: str = "",
        failures_file: Path | None = None,
        proxy_url: str = "",
        analytics_service: Any = None,
    ) -> None:
        self.bot_token = bot_token
        self.group_chat_id = group_chat_id
        self.session_store = session_store
        self.ws_manager = ws_manager
        self.clients_topic_id = clients_topic_id
        self.failures_file = failures_file
        # Опционально (None в части тестов) — используется для operator_claimed/operator_closed
        # событий, см. _track_operator_event. Аналитика "по манагерам" (в разработке,
        # 2026-08-27) без этого не имеет источника данных — до сих пор telegram_claimed_by жил
        # только в оперативной памяти сессии и стирался вместе с её TTL-эвикцией.
        self.analytics_service = analytics_service
        # Живой баг (2026-08-26): исходящий TCP по IPv6 с сервера не работает вообще, а по
        # IPv4 избирательно заблокирован именно диапазон адресов Telegram (149.154.x.x) —
        # подтверждено raw TCP тестами в обход MTU/DNS. HTTP-прокси (не SOCKS5 — не тянем
        # лишнюю зависимость socksio) снаружи этой сети чинит именно эту точку, ничего
        # больше не трогая.
        self.proxy_url = proxy_url or None
        self._api_base = f"https://api.telegram.org/bot{bot_token}"
        self._offset = 0

    def _record_failure(self, *, kind: str, session_id: str | None, data: dict[str, Any]) -> None:
        """Раньше неудачная отправка (кроме 429, тот ретраится в _call) оставляла только одну
        строчку в логе, которая укатывалась вниз консоли — не было способа потом узнать, дошла
        ли конкретная карточка. Это не переезд на DeliveryService (та ретрай/dead-letter машина
        рассчитана на разовые уведомления с фоновым повтором — для форвардинга живого сообщения
        клиент<->оператор бэкграунд-ретрай через минуту не всегда даже имеет смысл, тема
        отдельная, покрупнее) — просто честный durable-след для вопроса "а мы вообще узнаем?"."""

        if self.failures_file is None:
            return
        try:
            append_jsonl(
                self.failures_file,
                {
                    "timestamp": _iso(_utcnow()),
                    "kind": kind,
                    "session_id": session_id,
                    "error_code": data.get("error_code"),
                    "description": data.get("description"),
                },
            )
        except OSError as error:
            logger.warning("telegram_bridge failure_log_write_error error=%s", type(error).__name__)

    async def _track_operator_event(
        self, *, event_type: str, session: Any, claimed_by: str
    ) -> None:
        """operator_claimed/operator_closed — единственный durable источник "кто из
        операторов что обработал" (2026-08-27, аналитика "по манагерам" в разработке).
        session.telegram_claimed_by живёт только в памяти и стирается вместе с TTL-эвикцией
        сессии — без этого события история навсегда теряется через сутки-двое."""

        if self.analytics_service is None:
            return
        try:
            await self.analytics_service.track_event(
                company_id=session.company_id,
                session_id=session.session_id,
                event_type=event_type,
                metadata={"claimed_by": claimed_by},
            )
        except Exception as error:
            logger.warning(
                "telegram_bridge operator_event_track_failed event_type=%s session_id=%s error=%s",
                event_type,
                session.session_id,
                type(error).__name__,
            )

    async def close_evicted_operator_session(self, session: Any) -> None:
        """Живой баг (код-ревью, 2026-08-27; докручено 2026-08-29): сессия дошла до
        TTL-эвикции, пока была в работе у оператора (WAITING_OPERATOR/HUMAN_ACTIVE), и никто
        не закрыл её явным /done или клиентским уходом. Раньше только фиксировали
        operator_closed в аналитику (иначе telegram_claimed_by стирается вместе с сессией и
        operator_summary навсегда теряет этот диалог из числа "закрыто") — но сама тема в
        Telegram оставалась висеть открытой навсегда, никто не уведомлялся. Теперь ещё и
        закрываем тему тем же путём, что /done, с отдельной пометкой (не реальное завершение
        диалога, а автозакрытие по неактивности)."""

        operator_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}
        if getattr(session, "status", None) not in operator_statuses:
            return
        claimed_by = getattr(session, "telegram_claimed_by", None)
        if not claimed_by:
            return
        await self._track_operator_event(event_type="operator_closed", session=session, claimed_by=claimed_by)
        topic_id = getattr(session, "telegram_topic_id", None)
        if topic_id is not None:
            await self._close_session_from_topic(
                session.session_id,
                topic_id,
                closing_text="⏱ Диалог автоматически закрыт по неактивности (48 часов).",
            )

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.group_chat_id)

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        """Telegram лимитирует примерно 1 сообщение/сек в один и тот же чат — все карточки
        очереди операторов идут в одну группу, так что под конкурентной нагрузкой 429 ("Too
        Many Requests") — ожидаемый случай, не редкость. Раньше он тихо логировался и
        карточка терялась без следа (живой баг, найден нагрузочным тестом: 18 из 57 карточек
        не доходили при 10 параллельных запросах). Теперь уважаем retry_after, который сам
        Telegram присылает в ответе, и повторяем — вместо того чтобы просто потерять.

        Отдельно — короткий ретрай (несколько секунд, не фоновая очередь) на ЛЮБУЮ другую
        неудачу: сетевой сбой (ConnectError/Timeout) или не-429 ошибка API (409 Conflict от
        параллельного инстанса, 5xx). Раньше такое отправлялось один раз и терялось без следа."""

        data: dict[str, Any] = {}
        rate_limit_attempt = 0
        transient_attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, proxy=self.proxy_url) as client:
                    response = await client.post(f"{self._api_base}/{method}", json=params)
                    data = response.json()
            except httpx.HTTPError as error:
                if transient_attempt >= _MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        "telegram_bridge network_error method=%s error=%s attempt=%s/%s",
                        method,
                        type(error).__name__,
                        transient_attempt + 1,
                        _MAX_TRANSIENT_RETRIES + 1,
                    )
                    return {"ok": False, "description": f"network_error:{type(error).__name__}"}
                logger.warning(
                    "telegram_bridge network_error_retry method=%s error=%s attempt=%s/%s",
                    method,
                    type(error).__name__,
                    transient_attempt + 1,
                    _MAX_TRANSIENT_RETRIES + 1,
                )
                transient_attempt += 1
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                continue

            if data.get("ok"):
                return data

            retry_after = _telegram_retry_after(data)
            if retry_after is not None and rate_limit_attempt < _MAX_RATE_LIMIT_RETRIES:
                logger.warning(
                    "telegram_bridge rate_limited method=%s retry_after=%ss attempt=%s/%s",
                    method,
                    retry_after,
                    rate_limit_attempt + 1,
                    _MAX_RATE_LIMIT_RETRIES,
                )
                rate_limit_attempt += 1
                await asyncio.sleep(retry_after)
                continue

            if retry_after is None and transient_attempt < _MAX_TRANSIENT_RETRIES:
                logger.warning(
                    "telegram_bridge api_error_retry method=%s description=%s attempt=%s/%s",
                    method,
                    data.get("description"),
                    transient_attempt + 1,
                    _MAX_TRANSIENT_RETRIES + 1,
                )
                transient_attempt += 1
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                continue

            logger.warning(
                "telegram_bridge api_error method=%s description=%s",
                method,
                data.get("description"),
            )
            return data

    async def health_check(self) -> dict[str, Any]:
        """Side-effect-free проверка живости доставки — getMe + getChatMember, оба строго
        read-only методы Bot API (в отличие от sendMessage/createForumTopic, физически не
        могут произвести видимое сообщение ни в одном чате). Не входит в /health (см. его
        собственный докстринг — сетевой-free by design, дёргается внешним мониторингом каждые
        1-5 минут) — это отдельный, ручной прогон, например сразу после деплоя, чтобы не
        узнавать о мёртвой доставке только когда реальный лид не дошёл."""

        if not self.enabled:
            return {
                "enabled": False,
                "bot_token": {"status": "skip", "detail": "нет токена/группы — Telegram-бридж отключён"},
                "operators_group": {"status": "skip", "detail": "—"},
            }

        me = await self._call("getMe")
        if not me.get("ok"):
            return {
                "enabled": True,
                "bot_token": {"status": "error", "detail": str(me.get("description") or "getMe failed")},
                "operators_group": {"status": "skip", "detail": "не проверено — токен уже недействителен"},
            }

        bot_info = me.get("result") or {}
        member = await self._call("getChatMember", chat_id=self.group_chat_id, user_id=bot_info.get("id"))
        if not member.get("ok"):
            operators_group = {
                "status": "error",
                "detail": str(member.get("description") or "getChatMember failed"),
            }
        else:
            member_status = str((member.get("result") or {}).get("status") or "")
            if member_status in {"left", "kicked"}:
                operators_group = {
                    "status": "error",
                    "detail": f"бот больше не в группе (status={member_status})",
                }
            else:
                operators_group = {"status": "ok", "detail": f"доступ есть, status={member_status}"}

        return {
            "enabled": True,
            "bot_token": {"status": "ok", "detail": f"bot username: @{bot_info.get('username')}"},
            "operators_group": operators_group,
        }

    async def post_operator_queue_card(
        self,
        *,
        session_id: str,
        reason: str,
        last_message: str,
        client_label: str,
    ) -> None:
        """Карточка в General — очередь входящих, ждущих оператора. Тема сессии создаётся
        только при клейме (_handle_callback_query), не здесь."""

        if not self.enabled:
            return
        card_text = (
            f"{reason} — *{_escape_markdown(client_label)}*\n\n"
            f"💬 \"{_escape_markdown(last_message)}\"\n\n"
            "Нажмите «Взять в работу», чтобы начать переписку."
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "Взять в работу", "callback_data": f"{CLAIM_CALLBACK_PREFIX}{session_id}"}]
            ]
        }
        data = await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            text=card_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        if not data.get("ok"):
            self._record_failure(kind="operator_queue_card", session_id=session_id, data=data)

    async def post_client_lead_card(self, card_text: str, *, session_id: str = "") -> None:
        """Карточка в тему "Клиенты" — лид/запись без прямой необходимости в операторе.
        Без кнопки, без своей темы — просто лог."""

        if not self.enabled or not self.clients_topic_id:
            return
        data = await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=int(self.clients_topic_id),
            text=card_text,
            parse_mode="Markdown",
        )
        if not data.get("ok"):
            self._record_failure(kind="client_lead_card", session_id=session_id or None, data=data)

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

    async def close_topic(self, session_id: str, topic_id: Optional[int] = None) -> None:
        # topic_id опционален — 2026-08-29, для эвикции по TTL: к моменту вызова сессия уже
        # удалена из store (evict_stale отработал раньше), обычный self.session_store.get()
        # вернул бы None и молча ничего не закрыл бы. Явно переданный topic_id обходит это.
        if topic_id is None:
            session = await self.session_store.get(session_id)
            if session is None or session.telegram_topic_id is None:
                return
            topic_id = session.telegram_topic_id
        if not self.enabled:
            return
        await self._call(
            "closeForumTopic",
            chat_id=self.group_chat_id,
            message_thread_id=topic_id,
        )

    async def notify_client_left(self, session_id: str) -> None:
        """Клиент сам сбросил диалог кнопкой в виджете, пока ждал оператора или уже общался
        с ним — без этого оператор продолжал бы печатать в тему, не понимая, куда пропал
        собеседник (симметрично тому, как _close_session_from_topic уведомляет клиента,
        когда диалог завершает оператор)."""

        session = await self.session_store.get(session_id)
        if session is None or session.telegram_topic_id is None or not self.enabled:
            return
        await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=session.telegram_topic_id,
            text="⚠️ Клиент покинул чат и начал новый диалог.",
        )
        await self.close_topic(session_id)

    def _next_daily_topic_index(self, today: datetime) -> int:
        """Порядковый номер темы за сегодня — не отдельный счётчик в памяти (пропал бы при
        рестарте сервера, задваивая номера уже в первый же день), а количество
        operator_claimed-событий за сегодня в analytics.jsonl: тот же файл, что уже
        используется для intent_breakdown/unanswered_trend — read_jsonl там уже не проблема
        по объёму данных одной клиники, тот же trade-off, не новый.
        Без +1: к моменту вызова событие ТЕКУЩЕГО клейма уже дозаписано в файл
        (_track_operator_event вызывается раньше _create_session_topic в
        _handle_callback_query), так что подсчёт уже включает сам этот клейм."""

        analytics_file = getattr(self.analytics_service, "analytics_file", None)
        if analytics_file is None:
            return 1
        try:
            count = 0
            for event in read_jsonl(analytics_file):
                if event.get("event_type") != "operator_claimed":
                    continue
                timestamp = str(event.get("timestamp") or "")
                try:
                    event_time = datetime.fromisoformat(timestamp)
                except ValueError:
                    continue
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)
                if event_time.astimezone(_TOPIC_DISPLAY_TIMEZONE).date() == today.date():
                    count += 1
            return max(count, 1)
        except Exception as error:
            # Красивый номер в заголовке — не то, ради чего стоит рисковать самим клеймом:
            # любая неожиданность тут (битый файл, права на чтение) не должна ронять
            # реальное создание темы, только откатываемся на "номер 1".
            logger.warning("topic index computation failed error=%s", type(error).__name__)
            return 1

    def _topic_display_index(self) -> str:
        now = datetime.now(_TOPIC_DISPLAY_TIMEZONE)
        index = self._next_daily_topic_index(now)
        return f"#{now:%d%m%y}{index:03d}"

    async def _create_session_topic(self, session: Any, *, claimed_by: str) -> int | None:
        """Создаёт тему сессии в момент клейма — заголовок сразу содержит читаемый номер,
        эмодзи-тип обращения и оператора, не нужно отдельно переименовывать после."""

        emoji = TOPIC_TYPE_EMOJI.get(getattr(session, "last_intent", None), TOPIC_TYPE_EMOJI_DEFAULT)
        topic_name = f"{self._topic_display_index()} {emoji} · {claimed_by}"[:128]
        result = await self._call("createForumTopic", chat_id=self.group_chat_id, name=topic_name)
        if not result.get("ok"):
            return None

        topic_id = result["result"]["message_thread_id"]
        await self.session_store.set_telegram_bridge(session.session_id, topic_id=topic_id)

        transcript = _format_transcript(session)
        if transcript:
            # Без parse_mode: текст переписки — это данные пользователя, не наш markdown,
            # экранировать его ради Markdown-разметки не имеет смысла (может сломать парсинг).
            await self._call(
                "sendMessage",
                chat_id=self.group_chat_id,
                message_thread_id=topic_id,
                text=transcript[:4000],
            )
        # Кнопка + закреп — альтернатива набору /done руками (аудит удобства, 2026-08-24):
        # оператору из телефона неудобно печатать команду посреди диалога, а закреп держит
        # кнопку доступной даже в длинной переписке без прокрутки к самому началу темы.
        pin_result = await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=topic_id,
            text=f"🟢 Взято в работу — {claimed_by}",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "✅ Завершить диалог", "callback_data": f"{CLOSE_CALLBACK_PREFIX}{session.session_id}"}]
                ]
            },
        )
        pin_message_id = pin_result.get("result", {}).get("message_id") if pin_result.get("ok") else None
        if pin_message_id is not None:
            await self._call(
                "pinChatMessage",
                chat_id=self.group_chat_id,
                message_id=pin_message_id,
                disable_notification=True,
            )
        return topic_id

    async def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")

        if data.startswith(CLOSE_CALLBACK_PREFIX):
            session_id = data[len(CLOSE_CALLBACK_PREFIX) :]
            session = await self.session_store.get(session_id)
            if session is None or session.telegram_topic_id is None:
                if callback_id:
                    await self._call(
                        "answerCallbackQuery", callback_query_id=callback_id, text="Сессия не найдена"
                    )
                return
            await self._close_session_from_topic(session_id, session.telegram_topic_id)
            if callback_id:
                await self._call("answerCallbackQuery", callback_query_id=callback_id, text="Диалог завершён")
            return

        if not data.startswith(CLAIM_CALLBACK_PREFIX):
            return

        session_id = data[len(CLAIM_CALLBACK_PREFIX) :]
        from_user = callback.get("from") or {}
        username = operator_label(from_user)

        session = await self.session_store.get(session_id)
        if session is None:
            if callback_id:
                await self._call("answerCallbackQuery", callback_query_id=callback_id, text="Сессия не найдена")
            return

        # Живой баг (ручное тестирование пользователем, 2026-08-26): клиент мог уже сам
        # вернуться к боту (см. operator_wait_timeout_offer в chat_service.py), пока карточка
        # с кнопкой "Взять в работу" ещё висит непросмотренной. Без этой проверки поздний клик
        # молча выдёргивал живой AI-диалог обратно в HUMAN_ACTIVE — бот замолкал посреди
        # разговора, о котором клиент уже забыл попросить оператора.
        if session.status == SessionStatus.AI_ACTIVE:
            if callback_id:
                await self._call(
                    "answerCallbackQuery",
                    callback_query_id=callback_id,
                    text="Клиент уже вернулся к боту, помощь не нужна",
                )
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
        await self._track_operator_event(
            event_type="operator_claimed", session=session, claimed_by=username
        )
        # Живой баг (найден нагрузочным тестом виджета, 2026-08-25): клейм через Telegram-кнопку
        # менял только telegram_claimed_by/telegram_topic_id, но не статус сессии — в отличие
        # от веб-панели /operator (routes/operator.py:take_session), которая правильно ставит
        # HUMAN_ACTIVE. Сессия навсегда оставалась в WAITING_OPERATOR: каждое следующее
        # сообщение клиента получало "Ваше сообщение получено, администратор подключается"
        # вместо реального разговора, хотя оператор уже реально взял диалог в работу.
        if session.status != SessionStatus.CLOSED:
            await self.session_store.set_status(session_id, SessionStatus.HUMAN_ACTIVE)

        topic_id = session.telegram_topic_id
        if topic_id is None:
            topic_id = await self._create_session_topic(session, claimed_by=username)

        message = callback.get("message") or {}
        if message.get("message_id"):
            keyboard = [[{"text": f"Взято: {username}", "callback_data": "claimed_noop"}]]
            if topic_id is not None:
                keyboard.append(
                    [{"text": "Перейти к переписке →", "url": _topic_deep_link(self.group_chat_id, topic_id)}]
                )
            await self._call(
                "editMessageReplyMarkup",
                chat_id=self.group_chat_id,
                message_id=message["message_id"],
                reply_markup={"inline_keyboard": keyboard},
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

    async def _close_session_from_topic(
        self, session_id: str, thread_id: int, *, closing_text: str = "✅ Диалог завершён."
    ) -> None:
        """Завершает диалог по команде оператора из темы (/done и т.д.) — переиспользует
        тот же путь закрытия, что и веб-панель оператора (disconnect_operator), плюс
        закрывает саму тему. closing_text переопределяется для автозакрытия по TTL
        (close_evicted_operator_session) — отдельная пометка, чтобы оператор не путал
        автозакрытие по неактивности с реальным завершением диалога."""

        session = await self.session_store.get(session_id)
        if session is not None and session.telegram_claimed_by:
            await self._track_operator_event(
                event_type="operator_closed", session=session, claimed_by=session.telegram_claimed_by
            )
        await self.ws_manager.disconnect_operator(session_id, close_session=True)
        # Сообщение — до закрытия темы: Telegram не даёт постить в уже закрытую тему.
        await self._call(
            "sendMessage",
            chat_id=self.group_chat_id,
            message_thread_id=thread_id,
            text=closing_text,
        )
        # topic_id передаём явно (не полагаемся на self.session_store.get внутри close_topic) —
        # при вызове из эвикции сессии в сторе уже нет, только этим путём тема реально закроется.
        await self.close_topic(session_id, topic_id=thread_id)

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

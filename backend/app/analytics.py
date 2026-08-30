"""простая аналитика managed-service mvp."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .models import PolicyAction, PolicyReason, PolicyResult, Session
from .utils.jsonl import read_jsonl


logger = logging.getLogger(__name__)


UNKNOWN_REASONS = {
    PolicyReason.UNKNOWN_SERVICE,
    PolicyReason.PRICE_QUESTION_NO_SERVICE,
    PolicyReason.SIMILAR_SERVICES_FOUND,
}
# message_answered пишется на КАЖДЫЙ ход (track_answer) и не имеет ценности после
# отладочного окна — единственный event_type, который archive_old_analytics_events
# сворачивает в счётчики и удаляет. Остальные типы (unknown_question/regulated_handoff/
# operator_requested/...) хранятся без ограничения — там сырой текст полезен и спустя месяцы.
# widget_impression/chat_opened (2026-08-27, воронка конверсии) — та же логика: высокий
# объём (пишется на каждую загрузку страницы/каждое открытие чата), без индивидуальной
# ценности после недолгого окна — сворачиваем туда же.
MESSAGE_RETENTION_EVENT_TYPES = {"message_answered", "widget_impression", "chat_opened"}
# Воронка (conversion_funnel) джойнит эти событий по session_id, а после ретеншна сырой
# session_id пропадает (rollup хранит только счётчики) — окно воронки держим заметно
# КОРОЧЕ ретеншна (60 дней), чтобы каждая стадия всегда считалась по ещё живым сырым
# записям, а не молча деградировала на старых данных.
FUNNEL_WINDOW_DAYS = 30
WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _within_range(
    entries: list[dict[str, Any]],
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    company_id: Optional[str],
) -> list[dict[str, Any]]:
    """Общий фильтр "компания + временной диапазон" — обобщение _within_days на явные
    границы (2026-08-30, кастомный период на дашборде). start/end независимы: только start
    = "не раньше X", только end = "не позже Y", оба None = без ограничения по дате вообще."""

    def _keep(entry: dict[str, Any]) -> bool:
        if company_id is not None and entry.get("company_id") != company_id:
            return False
        if start is None and end is None:
            return True
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp")))
        except (TypeError, ValueError):
            return False
        if start is not None and timestamp < start:
            return False
        if end is not None and timestamp > end:
            return False
        return True

    return [entry for entry in entries if _keep(entry)]


def _within_days(
    entries: list[dict[str, Any]], *, days: Optional[int], company_id: Optional[str]
) -> list[dict[str, Any]]:
    """Общий фильтр "компания + не старше N дней" — переиспользуется везде, где раньше
    каждый метод сам городил свою версию (2026-08-27, общий date-range фильтр на дашборде).
    days=None — без ограничения по дате (только company_id, если задан). Частный случай
    _within_range: скользящее окно от "сейчас", без верхней границы — сохранён как отдельная
    функция ради обратной совместимости (много вызовов/тестов уже завязаны на days=int)."""

    start = datetime.utcnow() - timedelta(days=days) if days is not None else None
    return _within_range(entries, start=start, end=None, company_id=company_id)


def _resolve_range(
    *, days: Optional[int], start: Optional[datetime], end: Optional[datetime]
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Явные start/end (кастомный период, 2026-08-30) побеждают days, если хоть один задан.
    Иначе — старое поведение days: скользящее окно от "сейчас", без верхней границы.
    days=None и start/end оба None — вообще без ограничения по дате ("всё время")."""

    if start is not None or end is not None:
        return start, end
    if days is not None:
        return datetime.utcnow() - timedelta(days=days), None
    return None, None


class AnalyticsService:
    """пишет lightweight события и строит отчёт без БД."""

    def __init__(
        self,
        analytics_file: Path,
        leads_file: Path,
        rollup_file: Optional[Path] = None,
        leads_archive_file: Optional[Path] = None,
        conversations_archive_file: Optional[Path] = None,
    ) -> None:
        self.analytics_file = analytics_file
        self.leads_file = leads_file
        # Оба опциональны (None в части тестов, которые их не касаются) — нужны только для
        # трендов, переживающих ретеншн: rollup_file хранит дневные+часовые счётчики после
        # того, как сырые message_answered/widget_impression/chat_opened уже удалены (см.
        # archive_old_analytics_events), leads_archive_file — лиды старше 90 дней, унесённые
        # из "горячего" файла (см. archive_old_leads в leads.py), но не удалённые.
        self.rollup_file = rollup_file
        self.leads_archive_file = leads_archive_file
        # 2026-08-29: полная переписка сессий, уже эвиктнутых из памяти — см.
        # sessions.archive_session. list_conversations/get_conversation читают отсюда то,
        # чего уже нет в live session_store (вкладка "Чаты" смотрит и туда, и сюда разом).
        self.conversations_archive_file = conversations_archive_file

    def _all_leads(self) -> list[dict[str, Any]]:
        """Горячий файл + архив вместе — иначе любой лидовый агрегат (по месяцам, по услуге,
        по типу) молча теряет всё старше leads_retention_days (90 дней по умолчанию), как
        только archive_old_leads реально начинает что-то переносить (живой баг, 2026-08-27:
        замечен на графике "Лиды по месяцам", когда система проработает полгода)."""

        leads = read_jsonl(self.leads_file)
        if self.leads_archive_file is not None:
            leads = leads + read_jsonl(self.leads_archive_file)
        return leads

    async def track_event(
        self,
        *,
        company_id: str,
        session_id: str,
        event_type: str,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.analytics_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "company_id": company_id,
            "session_id": session_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        }
        with self.analytics_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def track_answer(
        self,
        *,
        company_id: str,
        session_id: str,
        message: str,
        answer: str,
        action: str,
        policy_reason: Optional[str] = None,
    ) -> None:
        """Пишет КАЖДЫЙ обмен репликами (не только исключения), чтобы разбор "бот ответил
        ерунду" был по логу, а не по памяти или повторному прогону через LLM (она
        недетерминирована — повтор через /api/debug/trace может дать другой ответ)."""

        await self.track_event(
            company_id=company_id,
            session_id=session_id,
            event_type="message_answered",
            message=message,
            metadata={"answer": answer, "action": action, "policy_reason": policy_reason},
        )

    async def track_policy_result(
        self,
        *,
        company_id: str,
        session_id: str,
        message: str,
        policy_result: PolicyResult,
    ) -> None:
        metadata = {
            "action": policy_result.action.value,
            "reason": policy_result.reason.value,
            "service_id": policy_result.service_id,
        }
        if policy_result.reason in UNKNOWN_REASONS:
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="unknown_question",
                message=message,
                metadata=metadata,
            )
        if policy_result.reason == PolicyReason.REGULATED_ADVICE:
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="regulated_handoff",
                message=message,
                metadata=metadata,
            )
        if (
            policy_result.action == PolicyAction.TRANSFER_OPERATOR
            or policy_result.reason == PolicyReason.OPERATOR_REQUESTED
        ):
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="operator_requested",
                message=message,
                metadata=metadata,
            )
        if policy_result.reason in {PolicyReason.OBJECTION_HANDLED, PolicyReason.OBJECTION_BACKOFF}:
            # 2026-08-29: только инструментация — до сих пор конкретная ТЕМА возражения
            # (price/hesitation/competitor/guarantee/pain_fear) нигде не логировалась долговечно,
            # только общий policy_reason (см. message_answered) и счётчик в памяти сессии
            # (session.objection_response_counts, пропадает вместе с TTL). Отчёт "топ возражений
            # по теме" пока не строим — данных с этим полем ещё нет, копим с сегодня.
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="objection_raised",
                message=message,
                metadata={**metadata, "objection_topic": policy_result.safe_context.get("objection_topic")},
            )

    def summary(
        self,
        sessions: list[Session],
        company_id: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        filtered_sessions = [
            session for session in sessions if company_id is None or session.company_id == company_id
        ]
        leads = [
            lead
            for lead in self._all_leads()
            if company_id is None or lead.get("company_id") == company_id
        ]
        events = [
            event
            for event in read_jsonl(self.analytics_file)
            if company_id is None or event.get("company_id") == company_id
        ]

        sessions_by_status = Counter(session.status.value for session in filtered_sessions)
        sessions_by_company = Counter(session.company_id for session in filtered_sessions)
        leads_by_company = Counter(str(lead.get("company_id") or "legacy") for lead in leads)
        events_by_type = Counter(str(event.get("event_type") or "unknown") for event in events)
        events_by_company: dict[str, Counter[str]] = defaultdict(Counter)
        for event in events:
            event_company_id = str(event.get("company_id") or "unknown")
            event_type = str(event.get("event_type") or "unknown")
            events_by_company[event_company_id][event_type] += 1

        unanswered = [
            {
                "timestamp": event.get("timestamp"),
                "company_id": event.get("company_id"),
                "session_id": event.get("session_id"),
                "message": event.get("message"),
                "reason": (event.get("metadata") or {}).get("reason"),
                "service_id": (event.get("metadata") or {}).get("service_id"),
            }
            for event in reversed(events)
            if event.get("event_type") == "unknown_question"
        ][:limit]

        return {
            "company_id": company_id,
            "sessions": {
                "total": len(filtered_sessions),
                "by_status": dict(sessions_by_status),
                "by_company": dict(sessions_by_company),
                "operator_requested": sum(1 for session in filtered_sessions if session.operator_requested),
                "lead_requested": sum(1 for session in filtered_sessions if session.lead_requested),
                "messages_total": sum(len(session.messages) for session in filtered_sessions),
            },
            "leads": {
                "total": len(leads),
                "by_company": dict(leads_by_company),
            },
            "events": {
                "total": len(events),
                "by_type": dict(events_by_type),
                "by_company": {
                    event_company_id: dict(counter)
                    for event_company_id, counter in events_by_company.items()
                },
            },
            "unanswered": unanswered,
        }

    def operator_summary(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Аналитика "по манагерам" (2026-08-27) — считает operator_claimed/operator_closed
        события (см. telegram_bridge._track_operator_event) и attribute'ит лиды к оператору
        через тот же session_id И временное окно claim→close (2026-08-29: лид засчитывается
        оператору, только если случился реально пока диалог был у него в работе — иначе идёт
        в синтетическую запись "Бот", см. _operator_for_lead), ничего не меняя в самой модели
        Lead (связи в другую сторону в данных нет — джойним тут, на чтении, не на записи).
        days — общий date-range фильтр дашборда (None = за всё время). start/end (2026-08-30,
        кастомный период) — явные границы, побеждают days, если заданы."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        all_events = read_jsonl(self.analytics_file)
        events = _within_range(all_events, start=range_start, end=range_end, company_id=company_id)
        claims = [event for event in events if event.get("event_type") == "operator_claimed"]
        # closes/leads: живой баг (код-ревью, 2026-08-27) — раньше closes и leads фильтровались
        # ПО ОТДЕЛЬНОСТИ тем же days-окном, что и claims. Клейм внутри окна, чьё закрытие или
        # лид легли СНАРУЖИ окна (обычное дело у границы — сессию заклеймили под конец окна,
        # закрыли/лид создался чуть позже), молча терялся из "закрыто"/"лидов" оператора, хотя
        # сам claim честно посчитан. Джойн по session_id — это связь по факту, не по совпадению
        # дат: closes/leads берём из ПОЛНОЙ истории, а какие session_id вообще "в работе в этом
        # окне" решают только claims (единственное, что реально должно фильтроваться по days).
        close_by_session: dict[str, dict[str, Any]] = {}
        for event in all_events:
            if event.get("event_type") != "operator_closed":
                continue
            if company_id is not None and event.get("company_id") != company_id:
                continue
            # последнее закрытие на сессию — на случай повторных /done (не должно случаться в
            # норме, но не даёт задвоить duration, если всё-таки прилетело)
            close_by_session[str(event.get("session_id"))] = event

        # Живой баг (ручное тестирование пользователем, 2026-08-29): раньше лид засчитывался
        # ЛЮБОМУ, кто когда-либо клеймил эту сессию — включая лид, который бот сам собрал ДО
        # клейма (человек ещё даже не подключился), просто потому что оператор позже забрал
        # тот же диалог по совершенно другому поводу. Нечестно: оператор получал кредит за
        # работу бота. Теперь лид засчитывается оператору, только если он реально случился
        # МЕЖДУ его claim и close этой сессии (сессию можно клеймить несколько раз за жизнь —
        # берём клейм, актуальный на момент лида, не первый и не последний). Всё, что вне
        # какого-либо окна (до первого клейма или после закрытия) — идёт в "Бот", не теряется
        # молча и не приписывается случайному оператору.
        claims_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in claims:
            claims_by_session[str(claim.get("session_id"))].append(claim)
        for session_claims in claims_by_session.values():
            session_claims.sort(key=lambda claim: str(claim.get("timestamp") or ""))

        # Отдельный, полный (не last-wins, в отличие от close_by_session выше — тот годится
        # только для claimed/closed/avg_dialog_minutes per-claim, где задвоение не страшно)
        # список закрытий на сессию — нужен, чтобы найти ИМЕННО то закрытие, которое реально
        # завершает окно активного клейма, а не последнее закрытие сессии вообще. Без этого
        # переклейм после close (сессию заново эскалировали) ошибочно считал бы её уже
        # закрытой на момент лида, случившегося уже во ВТОРОМ, ещё открытом окне.
        closes_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in all_events:
            if event.get("event_type") != "operator_closed":
                continue
            if company_id is not None and event.get("company_id") != company_id:
                continue
            closes_by_session[str(event.get("session_id"))].append(event)
        for session_closes in closes_by_session.values():
            session_closes.sort(key=lambda close: str(close.get("timestamp") or ""))

        def _operator_for_lead(lead: dict[str, Any]) -> Optional[str]:
            session_id = str(lead.get("session_id"))
            session_claims = claims_by_session.get(session_id)
            if not session_claims:
                return None
            try:
                lead_at = datetime.fromisoformat(str(lead.get("timestamp")))
            except (TypeError, ValueError):
                # Не смогли понять, когда случился лид — не гадаем, кому его приписать.
                return None
            active_claim: Optional[dict[str, Any]] = None
            active_claimed_at: Optional[datetime] = None
            for claim in session_claims:
                try:
                    claimed_at = datetime.fromisoformat(str(claim.get("timestamp")))
                except (TypeError, ValueError):
                    continue
                if claimed_at <= lead_at:
                    active_claim, active_claimed_at = claim, claimed_at
                else:
                    break
            if active_claim is None:
                return None
            # Первое закрытие НЕ РАНЬШЕ активного клейма — то самое, что завершает именно
            # его окно (закрытия от более старых циклов клейм-close той же сессии пропускаем).
            for close in closes_by_session.get(session_id, []):
                try:
                    closed_at = datetime.fromisoformat(str(close.get("timestamp")))
                except (TypeError, ValueError):
                    continue
                if closed_at < active_claimed_at:
                    continue
                if closed_at < lead_at:
                    return None
                break
            return str((active_claim.get("metadata") or {}).get("claimed_by") or "unknown")

        leads_by_operator: Counter[str] = Counter()
        bot_leads = 0
        for lead in self._all_leads():
            if company_id is not None and lead.get("company_id") != company_id:
                continue
            operator = _operator_for_lead(lead)
            if operator:
                leads_by_operator[operator] += 1
            else:
                bot_leads += 1

        per_operator: dict[str, dict[str, Any]] = {}
        for claim in claims:
            operator = str((claim.get("metadata") or {}).get("claimed_by") or "unknown")
            stats = per_operator.setdefault(
                operator,
                {"claimed": 0, "closed": 0, "leads": leads_by_operator.get(operator, 0), "_durations": []},
            )
            stats["claimed"] += 1
            close = close_by_session.get(str(claim.get("session_id")))
            if close is not None:
                stats["closed"] += 1
                try:
                    claimed_at = datetime.fromisoformat(str(claim.get("timestamp")))
                    closed_at = datetime.fromisoformat(str(close.get("timestamp")))
                    delta_seconds = (closed_at - claimed_at).total_seconds()
                    # Не должно случаться в норме (сессию нельзя переклеймить, не закрыв —
                    # см. session.telegram_claimed_by guard в telegram_bridge.py), но кривые
                    # данные не должны рисовать отрицательное "среднее время диалога" —
                    # это разрушает доверие к дашборду сильнее, чем просто пропуск точки.
                    if delta_seconds >= 0:
                        stats["_durations"].append(delta_seconds)
                except (TypeError, ValueError):
                    pass

        operators: dict[str, Any] = {}
        for operator, stats in per_operator.items():
            durations = stats.pop("_durations")
            stats["avg_dialog_minutes"] = round(sum(durations) / len(durations) / 60, 1) if durations else None
            operators[operator] = stats
        if bot_leads:
            # claimed/closed/avg_dialog_minutes не имеют смысла для бота — не оператор, не
            # берёт диалоги в работу, только считаем лиды, до которых руки оператора не
            # дошли (или ещё не дошли, или уже отпустил).
            operators["Бот"] = {
                "claimed": 0,
                "closed": 0,
                "leads": bot_leads,
                "avg_dialog_minutes": None,
            }

        return {"company_id": company_id, "operators": operators}

    def leads_by_month(self, company_id: Optional[str] = None, months: int = 6) -> list[dict[str, Any]]:
        """Последние `months` календарных месяцев по счёту лидов, включая пустые (0) — иначе
        график молча пропускает провалы вместо того, чтобы их показать."""

        leads = [
            lead
            for lead in self._all_leads()
            if company_id is None or lead.get("company_id") == company_id
        ]
        counts: Counter[str] = Counter()
        for lead in leads:
            month_key = str(lead.get("timestamp") or "")[:7]
            if month_key:
                counts[month_key] += 1

        now = datetime.utcnow()
        result: list[dict[str, Any]] = []
        for offset in range(months - 1, -1, -1):
            year, month_num = now.year, now.month - offset
            while month_num <= 0:
                month_num += 12
                year -= 1
            key = f"{year:04d}-{month_num:02d}"
            result.append({"month": key, "count": counts.get(key, 0)})
        return result

    def top_services(
        self,
        company_id: Optional[str] = None,
        limit: int = 8,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Топ услуг по числу лидов — service_id без человекочитаемого имени (аналитика не
        знает о KnowledgeBase намеренно, имя резолвит вызывающий route, у которого он есть)."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        leads = _within_range(self._all_leads(), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter()
        for lead in leads:
            service_id = lead.get("service_id")
            if service_id:
                counts[str(service_id)] += 1
        return [{"service_id": service_id, "count": count} for service_id, count in counts.most_common(limit)]

    def leads_by_reason(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Разбивка лидов по reason (booking/price_question/medical_risk/commercial_interest,
        см. classify_lead_reason в leads.py) — уже есть на каждом Lead, новых полей не надо,
        для донат-чарта "какие лиды приходят"."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        leads = _within_range(self._all_leads(), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter(str(lead.get("reason") or "commercial_interest") for lead in leads)
        return [{"reason": reason, "count": count} for reason, count in counts.most_common()]

    def leads_feed(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        reason: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Построчный список лидов БЕЗ персональных данных — "уровень 0" таблицы лидов
        (2026-08-30, обсуждено с пользователем: минимальный показ данных, вместо ожидания
        решения клиента про имя/телефон). Намеренно НЕ читает и не отдаёт name/phone/summary/
        recent_messages — это не "скрыто на фронте", их физически нет в этом ответе. Так и
        должно оставаться, пока клиент явно не разрешит уровень 1 (см. память
        project_rosh_analytics_dashboard_backlog) — тогда это будет отдельный, осознанный
        аддитивный шаг (ещё 2 поля), не переделка."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        leads = _within_range(self._all_leads(), start=range_start, end=range_end, company_id=company_id)
        if reason is not None:
            leads = [lead for lead in leads if str(lead.get("reason") or "commercial_interest") == reason]
        leads.sort(key=lambda lead: str(lead.get("timestamp") or ""), reverse=True)
        return [
            {
                "timestamp": lead.get("timestamp"),
                "session_id": lead.get("session_id"),
                "service_id": lead.get("service_id"),
                "reason": str(lead.get("reason") or "commercial_interest"),
                "needs_operator": bool(lead.get("needs_operator")),
                "lead_trigger": str(lead.get("lead_trigger") or "ask_contact"),
            }
            for lead in leads[:limit]
        ]

    def intent_breakdown(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Разбивка по категориям сообщений (smalltalk/price_question/off_topic и т.д.) — для
        вкладки "Чаты" (TSK-05). Данные уже есть в message_answered.metadata.policy_reason
        (см. track_answer) — только агрегация, новой инструментации не потребовалось."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter()
        for event in events:
            if event.get("event_type") != "message_answered":
                continue
            reason = str((event.get("metadata") or {}).get("policy_reason") or "unknown")
            counts[reason] += 1
        return [{"reason": reason, "count": count} for reason, count in counts.most_common()]

    def objection_breakdown(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Разбивка возражений по теме (price/hesitation/competitor/guarantee/pain_fear) —
        тот же паттерн, что intent_breakdown выше, только по objection_raised.metadata.
        objection_topic (инструментация 2026-08-29, см. track_policy_result) — данные
        копятся с этой даты, историю назад не восстановить."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter()
        for event in events:
            if event.get("event_type") != "objection_raised":
                continue
            topic = str((event.get("metadata") or {}).get("objection_topic") or "unknown")
            counts[topic] += 1
        return [{"topic": topic, "count": count} for topic, count in counts.most_common()]

    def _archived_conversations(self, company_id: Optional[str] = None) -> list[dict[str, Any]]:
        if self.conversations_archive_file is None:
            return []
        return [
            record
            for record in read_jsonl(self.conversations_archive_file)
            if company_id is None or record.get("company_id") == company_id
        ]

    def list_conversations(
        self,
        live_sessions: list[Session],
        *,
        company_id: Optional[str] = None,
        scope: str = "all",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Вкладка "Чаты" (TSK-05): список диалогов — живые сессии (session_store, последние
        24-48ч) + уже заархивированные (conversations_archive.jsonl, см. sessions.archive_session)
        одним списком, отсортированным по свежести. scope: all | bot_only | operator | lead —
        напрямую по уже существующим полям (operator_requested/lead_requested), новых данных
        собирать не пришлось."""

        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for session in live_sessions:
            if company_id is not None and session.company_id != company_id:
                continue
            items.append(
                {
                    "session_id": session.session_id,
                    "company_id": session.company_id,
                    "status": session.status.value,
                    "operator_requested": session.operator_requested,
                    "lead_requested": session.lead_requested,
                    "message_count": len(session.messages),
                    "last_message": session.messages[-1].text if session.messages else "",
                    "updated_at": session.updated_at.isoformat(),
                    "source": "live",
                }
            )
            seen_ids.add(session.session_id)

        for record in self._archived_conversations(company_id=company_id):
            session_id = str(record.get("session_id") or "")
            if not session_id or session_id in seen_ids:
                continue
            messages = record.get("messages") or []
            items.append(
                {
                    "session_id": session_id,
                    "company_id": record.get("company_id"),
                    "status": record.get("status"),
                    "operator_requested": bool(record.get("operator_requested")),
                    "lead_requested": bool(record.get("lead_requested")),
                    "message_count": len(messages),
                    "last_message": messages[-1].get("text") if messages else "",
                    "updated_at": record.get("closed_at"),
                    "source": "archive",
                }
            )

        def _matches_scope(item: dict[str, Any]) -> bool:
            if scope == "bot_only":
                return not item["operator_requested"]
            if scope == "operator":
                return item["operator_requested"]
            if scope == "lead":
                return item["lead_requested"]
            return True

        filtered = [item for item in items if _matches_scope(item)]
        filtered.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return filtered[:limit]

    def get_conversation(self, session_id: str, live_sessions: list[Session]) -> Optional[dict[str, Any]]:
        """Полный транскрипт одного диалога для вкладки "Чаты" — сначала живая сессия, иначе
        ищем в архиве (см. list_conversations)."""

        for session in live_sessions:
            if session.session_id == session_id:
                return {
                    "session_id": session.session_id,
                    "company_id": session.company_id,
                    "status": session.status.value,
                    "operator_requested": session.operator_requested,
                    "lead_requested": session.lead_requested,
                    "messages": [
                        {
                            "role": message.role.value,
                            "text": message.text,
                            "kind": message.kind,
                            "created_at": message.created_at.isoformat(),
                        }
                        for message in session.messages
                    ],
                    "source": "live",
                }
        for record in self._archived_conversations():
            if record.get("session_id") == session_id:
                record = dict(record)
                record["source"] = "archive"
                return record
        return None

    def unanswered_trend(
        self,
        company_id: Optional[str] = None,
        days: int = 14,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Дневная динамика unknown_question — растёт база знаний или деградирует. Не
        подчиняется MESSAGE_RETENTION_EVENT_TYPES (хранится вечно), так что честно работает и
        для длинных окон, в отличие от unanswered_trend/conversion_funnel-класса метрик.

        Кастомный период (2026-08-30): start и end нужны ОБА, чтобы честно нарисовать ось
        дней — без верхней границы непонятно, на каком дне заканчивать ряд. Если задан только
        один из них или ни одного — старое поведение, последние `days` дней, кончая сегодня."""

        now = datetime.utcnow()
        if start is not None and end is not None:
            range_start, range_end = start, end
        else:
            # Живой баг (код-ревью, 2026-08-30): было `now - timedelta(days=days-1)` без
            # округления до полуночи — первый отображаемый день терял события до текущего
            # часа (старый код по факту брал cutoff на день шире дисплея, гарантируя запас;
            # тут запаса не было). Округляем начало вниз до 00:00 — конец (range_end=now)
            # намеренно НЕ округляем, "сегодня ещё не закончилось" — это честно, не баг.
            range_end = now
            range_start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter()
        for event in events:
            if event.get("event_type") != "unknown_question":
                continue
            day_key = str(event.get("timestamp") or "")[:10]
            if day_key:
                counts[day_key] += 1

        result: list[dict[str, Any]] = []
        day = range_start.date()
        last_day = range_end.date()
        while day <= last_day:
            key = day.strftime("%Y-%m-%d")
            result.append({"date": key, "count": counts.get(key, 0)})
            day += timedelta(days=1)
        return result

    def _top_messages_by_text(
        self,
        event_type: str,
        *,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 10,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Топ-N по частоте текста сообщения — группировка по НОРМАЛИЗОВАННОМУ (lower+strip)
        тексту, ловит буквальные повторы одного вопроса. Разные формулировки одного смысла
        ("сколько стоит ботокс" vs "почём ботокс") НЕ объединяются — это отдельная задача
        (кластеризация по смыслу), сознательно не делаем сейчас, см. память по TSK-05."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[str] = Counter()
        examples: dict[str, str] = {}
        for event in events:
            if event.get("event_type") != event_type:
                continue
            message = str(event.get("message") or "").strip()
            if not message:
                continue
            key = message.lower()
            counts[key] += 1
            examples.setdefault(key, message)
        return [{"message": examples[key], "count": count} for key, count in counts.most_common(limit)]

    def top_unanswered_questions(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 10,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        return self._top_messages_by_text(
            "unknown_question", company_id=company_id, days=days, limit=limit, start=start, end=end
        )

    def top_answered_questions(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 10,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        return self._top_messages_by_text(
            "message_answered", company_id=company_id, days=days, limit=limit, start=start, end=end
        )

    def _rollup_message_answered_rows(
        self,
        company_id: Optional[str],
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """rollup_file строками "дата+час" для message_answered — переживает ретеншн
        analytics_file (2026-08-27). event_type пишется явно с сегодняшнего дня; строка без
        него (гипотетический старый формат, до этого изменения ничего, кроме message_answered,
        в rollup не попадало) по умолчанию тоже считается message_answered.

        start/end (2026-08-30, кастомный период) — верхняя граница нужна, если период
        заканчивается раньше "сейчас" (раньше был только нижний cutoff, без верхней границы —
        для скользящего окна от сегодня она и не нужна была)."""

        if self.rollup_file is None:
            return []
        start_date = start.strftime("%Y-%m-%d") if start is not None else None
        end_date = end.strftime("%Y-%m-%d") if end is not None else None
        rows = []
        for row in read_jsonl(self.rollup_file):
            if row.get("event_type", "message_answered") != "message_answered":
                continue
            if company_id is not None and row.get("company_id") != company_id:
                continue
            row_date = str(row.get("date") or "")
            if start_date is not None and row_date < start_date:
                continue
            if end_date is not None and row_date > end_date:
                continue
            rows.append(row)
        return rows

    def activity_by_hour(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Сколько сообщений приходит в каждый час суток (UTC) — для планирования смен
        операторов. Сырые message_answered (недавние) + rollup (то, что уже сжато под
        ретеншном) — иначе окно дальше 60 дней молча показывало бы неполную картину."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[int] = Counter()
        for event in events:
            if event.get("event_type") != "message_answered":
                continue
            try:
                hour = datetime.fromisoformat(str(event.get("timestamp"))).hour
            except (TypeError, ValueError):
                continue
            counts[hour] += 1
        for row in self._rollup_message_answered_rows(company_id, start=range_start, end=range_end):
            try:
                # Живой баг (код-ревью, 2026-08-27): int(count) раньше жил СНАРУЖИ этого
                # try/except (тот ловил только парсинг часа) — битое/нецелое значение count
                # в rollup-строке падало необработанным ValueError и валило весь
                # /api/analytics/dashboard, а не просто пропускало одну плохую строку.
                hour = int(str(row.get("hour") or ""))
                count = int(row.get("count") or 0)
            except ValueError:
                continue
            counts[hour] += count
        return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]

    def activity_by_weekday(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Та же идея, но по дню недели (0=Пн ... 6=Вс) — на пару с activity_by_hour. День
        недели не хранится отдельно ни в сырых событиях, ни в rollup — всегда вычисляется из
        даты, это чистая функция, дублировать в схеме незачем."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        counts: Counter[int] = Counter()
        for event in events:
            if event.get("event_type") != "message_answered":
                continue
            try:
                weekday = datetime.fromisoformat(str(event.get("timestamp"))).weekday()
            except (TypeError, ValueError):
                continue
            counts[weekday] += 1
        for row in self._rollup_message_answered_rows(company_id, start=range_start, end=range_end):
            try:
                # Живой баг (код-ревью, 2026-08-27): та же дыра, что в activity_by_hour —
                # int(count) вне try/except мог уронить весь дашборд на одной плохой строке.
                weekday = datetime.strptime(str(row.get("date")), "%Y-%m-%d").weekday()
                count = int(row.get("count") or 0)
            except (TypeError, ValueError):
                continue
            counts[weekday] += count
        return [
            {"weekday": weekday, "label": WEEKDAY_LABELS[weekday], "count": counts.get(weekday, 0)}
            for weekday in range(7)
        ]

    def queue_wait_stats(
        self,
        company_id: Optional[str] = None,
        days: Optional[int] = None,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Сколько реально ждут оператора — от operator_requested (клиент попросил) до
        operator_claimed (кто-то взял в работу). Не путать с avg_dialog_minutes в
        operator_summary — та мерит claimed→closed, время самого разговора, не очереди.
        Оба события хранятся вечно (не в MESSAGE_RETENTION_EVENT_TYPES), так что это честно
        работает на любом окне, включая "всё время" (days=None)."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)

        requested_at_by_session: dict[str, datetime] = {}
        for event in events:
            if event.get("event_type") != "operator_requested":
                continue
            session_id = str(event.get("session_id") or "")
            if not session_id:
                continue
            try:
                timestamp = datetime.fromisoformat(str(event.get("timestamp")))
            except (TypeError, ValueError):
                continue
            # если клиент просил несколько раз подряд — считаем от первой просьбы, это
            # реальное время, которое он провёл в ожидании
            existing = requested_at_by_session.get(session_id)
            if existing is None or timestamp < existing:
                requested_at_by_session[session_id] = timestamp

        waits_minutes: list[float] = []
        for event in events:
            if event.get("event_type") != "operator_claimed":
                continue
            requested_at = requested_at_by_session.get(str(event.get("session_id") or ""))
            if requested_at is None:
                continue
            try:
                claimed_at = datetime.fromisoformat(str(event.get("timestamp")))
            except (TypeError, ValueError):
                continue
            delta_minutes = (claimed_at - requested_at).total_seconds() / 60
            if delta_minutes >= 0:
                waits_minutes.append(delta_minutes)

        return {
            "avg_wait_minutes": round(sum(waits_minutes) / len(waits_minutes), 1) if waits_minutes else None,
            "sample_size": len(waits_minutes),
        }

    def period_comparison(self, company_id: Optional[str] = None, days: int = 30) -> dict[str, Any]:
        """Текущий период vs такой же по длине предыдущий — для дельт на плитках ("+12% к
        прошлому периоду"). Не про воронку и не ограничено её более коротким safety-окном —
        conversations/leads тут те же самые, что даёт conversion_funnel, посчитанные дважды
        на два смежных отрезка."""

        now = datetime.utcnow()
        current_start = now - timedelta(days=days)
        previous_start = now - timedelta(days=days * 2)
        # Живой баг (код-ревью, 2026-08-27): conversations джойнит message_answered по
        # session_id — тот же ограничение, что у conversion_funnel (см. её докстринг):
        # сырые данные переживают только FUNNEL_WINDOW_DAYS. На большом `days` previous-окно
        # (days*2 назад) молча упирается в уже заархивированные (rollup, без session_id)
        # события и читает оттуда 0 — дельта на дашборде показывает ложный огромный "+X%".
        # Лидов это не касается — _all_leads() уже читает архив за любой период.
        conversation_days = min(days, FUNNEL_WINDOW_DAYS)
        conversation_current_start = now - timedelta(days=conversation_days)
        conversation_previous_start = now - timedelta(days=conversation_days * 2)

        def _in_range(entries: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
            result = []
            for entry in entries:
                if company_id is not None and entry.get("company_id") != company_id:
                    continue
                try:
                    timestamp = datetime.fromisoformat(str(entry.get("timestamp")))
                except (TypeError, ValueError):
                    continue
                if start <= timestamp < end:
                    result.append(entry)
            return result

        def _conversations(entries: list[dict[str, Any]]) -> int:
            return len(
                {
                    entry.get("session_id")
                    for entry in entries
                    if entry.get("event_type") == "message_answered" and entry.get("session_id")
                }
            )

        all_events = read_jsonl(self.analytics_file)
        all_leads = self._all_leads()

        current_conversations = _conversations(_in_range(all_events, conversation_current_start, now))
        previous_conversations = _conversations(
            _in_range(all_events, conversation_previous_start, conversation_current_start)
        )
        current_leads = len(_in_range(all_leads, current_start, now))
        previous_leads = len(_in_range(all_leads, previous_start, current_start))

        return {
            "days": days,
            # Реально применённое окно для conversations (после safety-зажима выше) — отдаём
            # честно, чтобы фронт не подписывал дельту неверным "(N дн.)", если N зажали.
            "conversations_days": conversation_days,
            "conversations": {"current": current_conversations, "previous": previous_conversations},
            "leads": {"current": current_leads, "previous": previous_leads},
        }

    def conversion_funnel(
        self,
        company_id: Optional[str] = None,
        days: int = FUNNEL_WINDOW_DAYS,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Воронка виджет-загружен → чат-открыт → есть-переписка → лид, за последние `days`
        дней (см. FUNNEL_WINDOW_DAYS — специально короче ретеншна widget_impression/
        chat_opened/message_answered, иначе стадии молча теряют сырые данные на границе окна).
        "Есть переписка" — уникальные session_id в message_answered, не отдельное событие:
        conversation is what happens when a real message gets answered, so a fresh event
        would only duplicate data already collected.

        start/end (2026-08-30, кастомный период) — вызывающий (routes/analytics.py) сам
        отвечает за клэмп 55-дневным окном, если диапазон шире — тут просто честно считаем
        по тому, что передали."""

        range_start, range_end = _resolve_range(days=days, start=start, end=end)
        events = _within_range(read_jsonl(self.analytics_file), start=range_start, end=range_end, company_id=company_id)
        leads = _within_range(self._all_leads(), start=range_start, end=range_end, company_id=company_id)

        impressions = sum(1 for event in events if event.get("event_type") == "widget_impression")
        chat_opened = sum(1 for event in events if event.get("event_type") == "chat_opened")
        conversations = len(
            {
                event.get("session_id")
                for event in events
                if event.get("event_type") == "message_answered" and event.get("session_id")
            }
        )
        lead_count = len(leads)

        stages = [
            {"label": "Виджет загружен", "count": impressions},
            {"label": "Чат открыт", "count": chat_opened},
            {"label": "Есть переписка", "count": conversations},
            {"label": "Стал лидом", "count": lead_count},
        ]
        # шаг-к-шагу конверсия (эта стадия / предыдущая), не от общего — так и читается
        # воронка визуально: "сколько из открывших чат реально написали"
        #
        # Живой баг (код-ревью, 2026-08-27): первая стадия раньше безусловно получала 100.0%
        # (ей не с чем сравнивать), даже когда impressions == 0 (трекинг показов только что
        # включили) — дашборд рисовал "0 (100.0%)" вместо честного "нет данных".
        for index, stage in enumerate(stages):
            if index == 0:
                stage["percent_of_previous"] = 100.0 if stage["count"] else None
                continue
            previous = stages[index - 1]["count"]
            stage["percent_of_previous"] = round(stage["count"] / previous * 100, 1) if previous else None

        # "Чат открыт" → "Есть переписка" сравнивает две метрики РАЗНОЙ надёжности: первые две
        # стадии — client-side маячки (fetch без keepalive на /api/analytics/track/... — то,
        # что чаще всего режут адблокеры), "Есть переписка" же считается на сервере по
        # message_answered и адблокером не режется. В проде это дало 7011.8% (обсуждено с
        # пользователем 2026-08-29) — не баг арифметики, а честно нечего сравнивать, проценты
        # с разных весов. Убираем именно ЭТОТ переход, остальные — Виджет→Чат (обе client-side)
        # и Переписка→Лид (обе server-side) — внутри своего яруса сравнимы, оставляем как есть.
        stages[2]["percent_of_previous"] = None

        # Честное число дней для хинта на дашборде ("За последние N дней") — не просто эхо
        # входного `days`: при кастомном периоде (2026-08-30), клэмпнутом до 55 дней вызывающим
        # (routes/analytics.py), это реально посчитанное окно, а не то, что запросил
        # пользователь. Живой баг найден при живой проверке: считать так же и для обычного
        # пресета (range_end здесь всегда None) даёт "31 день" вместо "30" — скользящее окно
        # "N дней назад от сейчас" считает ЧАСЫ (ровно N*24ч), а не календарные даты, разница
        # на дробный день округлением в date() и давала лишний +1. Пересчитываем ТОЛЬКО когда
        # range_end реально задан явно (кастом/клэмп) — иначе оставляем исходный `days` как есть.
        effective_days = (range_end.date() - range_start.date()).days + 1 if range_end is not None else days

        return {"company_id": company_id, "days": effective_days, "stages": stages}


def archive_old_analytics_events(analytics_file: Path, rollup_file: Path, retention_days: int) -> int:
    """удаляет записи MESSAGE_RETENTION_EVENT_TYPES старше retention_days из analytics_file,
    сохранив дневные счётчики (дата, company_id, action — без текста) в rollup_file.

    Остальные event_type не трогает — они остаются в analytics_file без ограничения.
    Возвращает количество удалённых записей. Ничего не делает (не трогает диск), если
    удалять нечего — обычный случай при ежедневном запуске.
    """

    entries = read_jsonl(analytics_file)
    if not entries:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    keep: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("event_type") not in MESSAGE_RETENTION_EVENT_TYPES:
            keep.append(entry)
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp")))
        except (TypeError, ValueError):
            keep.append(entry)
            continue
        (stale if timestamp < cutoff else keep).append(entry)

    if not stale:
        return 0

    rollup_counts: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for entry in stale:
        timestamp_str = str(entry.get("timestamp") or "")
        day = timestamp_str[:10] or "unknown"
        # Час — единственное измерение, которое стоило добавить в rollup сейчас (2026-08-27):
        # день недели однозначно вычисляется из даты при чтении, а вот час суток без него
        # теряется навсегда, как только сырые message_answered/widget_impression/chat_opened
        # уходят под ретеншн — activity_by_hour молча деградирует на длинных окнах.
        hour = timestamp_str[11:13] or "00"
        company_id = str(entry.get("company_id") or "unknown")
        # message_answered всегда несёт metadata.action (см. track_answer) — поведение для
        # него не меняется. widget_impression/chat_opened (2026-08-26) метаданных action не
        # имеют — раньше оба схлопнулись бы в один и тот же "unknown" рядом с message_answered
        # без action, теряя различимость; используем event_type как запасной ключ.
        event_type = str(entry.get("event_type") or "unknown")
        action = str((entry.get("metadata") or {}).get("action") or event_type or "unknown")
        rollup_counts[(day, hour, company_id, event_type, action)] += 1

    rollup_file.parent.mkdir(parents=True, exist_ok=True)
    with rollup_file.open("a", encoding="utf-8") as handle:
        for (day, hour, company_id, event_type, action), count in sorted(rollup_counts.items()):
            handle.write(
                _dump_jsonl_line(
                    {
                        "date": day,
                        "hour": hour,
                        "company_id": company_id,
                        "event_type": event_type,
                        "action": action,
                        "count": count,
                    }
                )
            )

    # атомарная перезапись "горячего" файла — временный файл + rename, чтобы конкурентный
    # append (новое сообщение пришло ровно во время чистки) не попал под усечение.
    tmp_path = analytics_file.with_suffix(analytics_file.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for entry in keep:
            handle.write(_dump_jsonl_line(entry))
    os.replace(tmp_path, analytics_file)

    logger.info(
        "analytics prune: removed %d of %d message_answered entries older than %d days (analytics_file=%s)",
        len(stale),
        len(entries),
        retention_days,
        analytics_file,
    )
    return len(stale)


def _dump_jsonl_line(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False) + "\n"

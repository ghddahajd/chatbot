"""внутренние debug-инструменты для демонстрации decision trace."""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import verify_operator_token
from ..models import Message, MessageRole, PolicyAction, PolicyReason, Session
from ..policy import classify_and_extract
from ..policy.restricted import is_restricted_question
from .chat_utils import (
    CONSULTATION_RISK_RESTRICTED,
    classify_consultation_risk,
    format_quick_actions,
    resolve_classification,
    safe_complete,
    safe_restricted_handoff,
    safe_small_talk,
    service_classifier_payload,
    should_use_consultation_llm,
)


router = APIRouter(tags=["debug"])


class DebugTraceRequest(BaseModel):
    company_id: str
    message: str = Field(min_length=1)


async def _final_answer_for_policy(
    request: Request,
    session: Session,
    message: str,
    policy_result,
    knowledge_base,
) -> tuple[str, PolicyAction, str, bool]:
    """повторяет answer-selection без сайд-эффектов ChatService."""

    if policy_result.action == PolicyAction.ASK_CONTACT:
        contact = policy_result.safe_context.get("contact")
        if contact:
            is_booking_request = bool(policy_result.safe_context.get("booking_request"))
            if is_booking_request:
                return (
                    "Спасибо. Заявку передали. С вами свяжутся, чтобы подтвердить время и детали.",
                    policy_result.action,
                    "direct_lead_preview",
                    True,
                )
            return (
                "Спасибо. Передали ваши контакты менеджеру. С вами свяжутся для уточнения деталей.",
                policy_result.action,
                "direct_lead_preview",
                True,
            )
        return str(policy_result.safe_context.get("message_to_user") or ""), policy_result.action, "direct", False

    if policy_result.action == PolicyAction.SMALL_TALK:
        return (
            await safe_small_talk(request, knowledge_base.company.company_name, message),
            policy_result.action,
            "small_talk",
            False,
        )

    if policy_result.action == PolicyAction.OFF_TOPIC:
        return str(policy_result.safe_context.get("message_to_user") or ""), policy_result.action, "direct", False

    if policy_result.action == PolicyAction.TRANSFER_OPERATOR:
        answer = str(
            policy_result.safe_context.get("handoff_message")
            or policy_result.safe_context.get("message_to_user")
            or "Передаю диалог менеджеру. Оператор увидит историю переписки."
        )
        return answer, policy_result.action, "direct_handoff", False

    if policy_result.action == PolicyAction.CLARIFY:
        direct_clarify_reasons = {
            PolicyReason.OPERATOR_REQUESTED,
            PolicyReason.LOCATION_MISMATCH,
            PolicyReason.UNSUPPORTED_CITY,
            PolicyReason.UNKNOWN_SERVICE,
            PolicyReason.SIMILAR_SERVICES_FOUND,
            PolicyReason.PRICE_QUESTION_NO_SERVICE,
            PolicyReason.SERVICE_EXPLANATION,
            PolicyReason.BOOKING_REQUEST,
            PolicyReason.CONTACT_PROVIDED,
        }
        if (
            policy_result.reason in direct_clarify_reasons
            or policy_result.safe_context.get("force_direct_answer")
            or policy_result.safe_context.get("message_to_user")
        ):
            answer = str(
                policy_result.safe_context.get("message_to_user")
                or policy_result.safe_context.get("city_note")
                or ""
            )
            return answer, policy_result.action, "direct", False
        return (
            await safe_complete(request, policy_result.safe_context, message, session.messages[-8:]),
            policy_result.action,
            "safe_complete",
            False,
        )

    if policy_result.action == PolicyAction.REJECT:
        return str(policy_result.safe_context.get("message_to_user") or "Запрос отклонён."), policy_result.action, "direct", False

    if (
        policy_result.action == PolicyAction.ANSWER
        and policy_result.safe_context.get("message_to_user")
        and not policy_result.safe_context.get("question_type")
        and not policy_result.safe_context.get("service")
        and not policy_result.safe_context.get("all_services")
    ):
        return str(policy_result.safe_context.get("message_to_user") or ""), policy_result.action, "direct", False

    if should_use_consultation_llm(policy_result.safe_context):
        consultation_risk, _request_id = await classify_consultation_risk(
            request,
            message,
            policy_result.safe_context,
        )
        if consultation_risk == CONSULTATION_RISK_RESTRICTED:
            return await safe_restricted_handoff(request, message), PolicyAction.TRANSFER_OPERATOR, "restricted_handoff", False
        return (
            await safe_complete(request, policy_result.safe_context, message, session.messages[-8:]),
            policy_result.action,
            "consultation_llm",
            False,
        )

    return (
        await safe_complete(request, policy_result.safe_context, message, session.messages[-8:]),
        policy_result.action,
        "safe_complete",
        False,
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


@router.post("/api/debug/trace")
async def debug_trace(
    payload: DebugTraceRequest,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    verify_operator_token(request, x_operator_token)
    started_at = time.perf_counter()
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")

    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(payload.company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    steps: list[dict[str, Any]] = []
    session = Session(company_id=payload.company_id)
    session.messages.append(Message(role=MessageRole.USER, text=message))
    known_services = service_classifier_payload(request, knowledge_base)

    local_classification = classify_and_extract(
        message,
        known_services,
        knowledge_base.company.city,
        knowledge_base.domain_profile,
    )
    classification_started_at = time.perf_counter()
    classification = await resolve_classification(message, request, knowledge_base)
    steps.append(
        {
            "step": "classification",
            "duration_ms": round((time.perf_counter() - classification_started_at) * 1000, 1),
            "result": {
                "local": local_classification,
                "final": classification,
            },
        }
    )

    is_restricted, restricted_category = is_restricted_question(message, knowledge_base.domain_profile)
    steps.append(
        {
            "step": "restricted_check",
            "result": {
                "is_restricted": is_restricted,
                "category": restricted_category,
                "domain_profile": knowledge_base.domain_profile,
            },
        }
    )

    service = knowledge_base.find_service_by_id(classification.get("service_id"))
    context = knowledge_base.get_service_context(service) if service else {}
    steps.append(
        {
            "step": "kb_lookup",
            "result": {
                "source": "services.json",
                "found": service is not None,
                "service_id": service.id if service else None,
                "service_name": service.name if service else None,
            },
        }
    )
    price = context.get("price") if isinstance(context.get("price"), dict) else None
    steps.append(
        {
            "step": "price_lookup",
            "result": {
                "source": "prices.json",
                "found": price is not None,
                "price_text": price.get("price_text") if price else None,
            },
        }
    )

    policy_started_at = time.perf_counter()
    policy_result = request.app.state.policy_analyzer(message, session, knowledge_base, classification)
    steps.append(
        {
            "step": "policy_decision",
            "duration_ms": round((time.perf_counter() - policy_started_at) * 1000, 1),
            "result": {
                "action": _enum_value(policy_result.action),
                "reason": _enum_value(policy_result.reason),
                "service_id": policy_result.service_id,
                "confidence": policy_result.confidence,
                "quick_actions": policy_result.quick_actions,
                "safe_context_keys": sorted(policy_result.safe_context.keys()),
            },
        }
    )

    generation_started_at = time.perf_counter()
    final_answer, response_action, generation_mode, lead_preview = await _final_answer_for_policy(
        request,
        session,
        message,
        policy_result,
        knowledge_base,
    )
    steps.append(
        {
            "step": "llm_generation",
            "duration_ms": round((time.perf_counter() - generation_started_at) * 1000, 1),
            "result": {
                "mode": generation_mode,
                "provider": request.app.state.settings.llm_provider,
                "model": request.app.state.settings.llm_model,
                "prompt_tokens": None,
                "completion_tokens": None,
            },
        }
    )
    steps.append(
        {
            "step": "validation",
            "result": {
                "passed": True,
                "note": "safe_complete handles validator/fallback internally",
            },
        }
    )

    return {
        "company_id": payload.company_id,
        "message": message,
        "steps": steps,
        "final_action": _enum_value(response_action),
        "final_answer": final_answer,
        "lead_preview": lead_preview,
        "quick_actions": [
            action.model_dump()
            for action in format_quick_actions(policy_result.quick_actions, request, knowledge_base)
        ],
        "total_time_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }


@router.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request) -> str:
    verify_operator_token(request, None)
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Debug trace</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 32px; background: #f6f3ee; color: #1f2922; }
    main { max-width: 960px; margin: 0 auto; }
    input, textarea, button { font: inherit; box-sizing: border-box; }
    input, textarea { width: 100%; padding: 12px; border: 1px solid #d8d0c4; border-radius: 12px; background: white; }
    textarea { min-height: 96px; margin-top: 12px; }
    button { margin-top: 12px; padding: 12px 18px; border: 0; border-radius: 12px; background: #1f7a5c; color: white; font-weight: 700; cursor: pointer; }
    pre { white-space: pre-wrap; background: #fff; border: 1px solid #e5dfd5; border-radius: 16px; padding: 16px; box-shadow: 0 2px 12px rgba(45,95,79,.08); }
  </style>
</head>
<body>
  <main>
    <h1>Debug trace</h1>
    <input id="company" value="rosh_demo" placeholder="company_id" />
    <textarea id="message" placeholder="Сообщение">сколько стоит чистка лица</textarea>
    <button id="run">Проверить</button>
    <pre id="output">Введите сообщение и нажмите Проверить.</pre>
  </main>
  <script>
    const token = new URLSearchParams(location.search).get("token") || "";
    document.getElementById("run").onclick = async () => {
      const output = document.getElementById("output");
      output.textContent = "Загрузка...";
      const response = await fetch(`/api/debug/trace?token=${encodeURIComponent(token)}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          company_id: document.getElementById("company").value,
          message: document.getElementById("message").value
        })
      });
      const payload = await response.json();
      output.textContent = JSON.stringify(payload, null, 2);
    };
  </script>
</body>
</html>
"""

"""внутренние debug-инструменты для демонстрации decision trace."""

from __future__ import annotations

import asyncio
import time
import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import verify_operator_token
from ..knowledge import normalize_text
from ..models import Message, MessageRole, PolicyAction, PolicyReason, Session
from ..policy import classify_and_extract
from ..policy.constants import DURATION_KEYWORDS, PRICE_KEYWORDS
from ..policy.extractors import contains_keyword
from ..policy.restricted import is_restricted_question
from ..services.rag_search import default_rag_chunks_path, retrieve_article_context, search_rag_chunks
from ..validator import validate_article_guidance_response
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


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


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
            or "Передаю диалог менеджеру. Он увидит историю переписки."
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

    if policy_result.action == PolicyAction.ANSWER and policy_result.safe_context.get("article_guidance_candidate"):
        candidate = policy_result.safe_context.get("article_guidance_candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        fallback = str(
            candidate.get("fallback_message_to_user") or policy_result.safe_context.get("message_to_user") or ""
        ).strip()
        excerpt = str(candidate.get("excerpt") or "").strip()
        if fallback and excerpt:
            llm_context = dict(policy_result.safe_context)
            llm_context["question_type"] = "article_guidance_excerpt"
            llm_context["article_guidance_candidate"] = candidate
            llm_context["article_context"] = [
                {
                    "title": str(candidate.get("title") or ""),
                    "url": str(candidate.get("url") or ""),
                    "snippet": excerpt,
                }
            ]
            llm_context.pop("message_to_user", None)
            answer = await safe_complete(request, llm_context, message, session.messages[-8:])
            if validate_article_guidance_response(answer, llm_context):
                return answer, policy_result.action, "article_guidance_llm", False
        return fallback, policy_result.action, "article_guidance_fallback", False

    if (
        policy_result.action == PolicyAction.ANSWER
        and policy_result.safe_context.get("message_to_user")
        and (
            policy_result.safe_context.get("force_direct_answer")
            or (
                not policy_result.safe_context.get("question_type")
                and not policy_result.safe_context.get("service")
                and not policy_result.safe_context.get("all_services")
            )
        )
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
    classification = await resolve_classification(message, request, knowledge_base, session)
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

    rag_started_at = time.perf_counter()
    rag_error = None
    article_matches: list[dict[str, Any]] = []
    normalized_message = normalize_text(message)
    price_requested = classification.get("intent") == "price_question" or contains_keyword(
        normalized_message, PRICE_KEYWORDS
    )
    duration_requested = contains_keyword(normalized_message, DURATION_KEYWORDS)
    rag_triggered = (
        classification.get("intent") == "faq_question"
        and not price_requested
        and not duration_requested
    )
    if rag_triggered:
        rag_query = f"{service.name} {message}" if service is not None else message
        try:
            article_matches = retrieve_article_context(rag_query)
        except FileNotFoundError:
            rag_error = f"corpus_not_found: {default_rag_chunks_path()}"
        except (json.JSONDecodeError, ValueError) as error:
            rag_error = f"invalid_corpus: {type(error).__name__}"
    steps.append(
        {
            "step": "rag_retrieval",
            "duration_ms": round((time.perf_counter() - rag_started_at) * 1000, 1),
            "result": {
                "triggered": rag_triggered,
                "matches": article_matches,
                "error": rag_error,
            },
        }
    )

    policy_started_at = time.perf_counter()
    policy_result = await asyncio.to_thread(
        request.app.state.policy_analyzer, message, session, knowledge_base, classification
    )
    rag_step = steps[-1]
    rag_step["result"]["used"] = policy_result.safe_context.get("question_type") == "faq_question"
    cosmetic_mapping = policy_result.safe_context.get("article_service_mapping")
    cosmetic_article_context = policy_result.safe_context.get("article_context")
    article_guidance_candidate = policy_result.safe_context.get("article_guidance_candidate")
    cosmetic_guidance_used = policy_result.safe_context.get("question_type") == "cosmetic_article_guidance"
    steps.append(
        {
            "step": "cosmetic_article_guidance",
            "result": {
                "used": cosmetic_guidance_used,
                "approved_mapping_found": bool(cosmetic_mapping),
                "excerpt_present": bool(
                    isinstance(article_guidance_candidate, dict)
                    and str(article_guidance_candidate.get("excerpt") or "").strip()
                ),
                "llm_candidate_available": isinstance(article_guidance_candidate, dict),
                "fallback_template": (
                    str(article_guidance_candidate.get("fallback_message_to_user") or "")
                    if isinstance(article_guidance_candidate, dict)
                    else None
                ),
                "mapping": cosmetic_mapping if isinstance(cosmetic_mapping, dict) else None,
                "matches": cosmetic_article_context if cosmetic_guidance_used else [],
            },
        }
    )
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


@router.post("/api/debug/rag-search")
async def debug_rag_search(
    payload: RagSearchRequest,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    verify_operator_token(request, x_operator_token)
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is empty")

    try:
        return search_rag_chunks(query=query, top_k=payload.top_k)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail=(
                "RAG chunks corpus not found. Run crawl_article_batch.py first "
                f"or set RAG_CHUNKS_FILE. Expected: {default_rag_chunks_path()}"
            ),
        ) from error
    except (json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=500, detail=f"Invalid RAG chunks corpus: {error}") from error


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
    :root {
      --bg: #f6f3ee;
      --card: #fffdf9;
      --ink: #1f2922;
      --muted: #6b7670;
      --line: #e5dfd5;
      --accent: #1f7a5c;
      --danger: #b45309;
      --shadow: 0 14px 40px rgba(45, 95, 79, .10);
    }
    * { box-sizing: border-box; }
    body {
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(31, 122, 92, .14), transparent 34rem),
        linear-gradient(135deg, #fbf8f2, var(--bg));
      color: var(--ink);
    }
    main { max-width: 1120px; margin: 0 auto; padding: 32px 20px 48px; }
    h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: -.04em; }
    p { margin: 0; color: var(--muted); }
    label { display: block; margin: 18px 0 8px; font-weight: 700; }
    input, textarea, button, select { font: inherit; }
    input, textarea, select {
      width: 100%;
      padding: 12px 14px;
      border: 1px solid #d8d0c4;
      border-radius: 14px;
      background: white;
      color: var(--ink);
    }
    textarea { min-height: 104px; resize: vertical; }
    button {
      border: 0;
      border-radius: 14px;
      background: var(--accent);
      color: white;
      font-weight: 800;
      cursor: pointer;
      padding: 12px 18px;
    }
    button.secondary { background: #e8f0ec; color: var(--accent); }
    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 20px; align-items: start; margin-top: 24px; }
    .panel, .card {
      background: rgba(255, 253, 249, .86);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }
    .panel { padding: 18px; position: sticky; top: 20px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
    .hint { margin-top: 12px; font-size: 13px; line-height: 1.45; color: var(--muted); }
    .samples { display: grid; gap: 8px; margin-top: 14px; }
    .sample {
      width: 100%;
      text-align: left;
      background: #fff;
      color: var(--ink);
      border: 1px solid var(--line);
      font-weight: 650;
      padding: 10px 12px;
    }
    .summary { padding: 18px; margin-bottom: 16px; }
    .summary h2 { margin: 0 0 10px; font-size: 20px; }
    .badges { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
    .badge {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      background: white;
      font-size: 13px;
      color: var(--muted);
    }
    .badge.strong { color: white; background: var(--accent); border-color: var(--accent); }
    .answer {
      margin-top: 12px;
      padding: 14px;
      border-radius: 16px;
      background: white;
      border: 1px solid var(--line);
      white-space: pre-wrap;
    }
    .steps { display: grid; gap: 12px; }
    .card { padding: 16px; }
    .card h3 { display: flex; justify-content: space-between; gap: 12px; margin: 0 0 10px; font-size: 16px; }
    .duration { color: var(--muted); font-size: 13px; font-weight: 500; }
    .kv { display: grid; grid-template-columns: 160px 1fr; gap: 6px 12px; font-size: 14px; }
    .kv div:nth-child(odd) { color: var(--muted); }
    pre {
      white-space: pre-wrap;
      overflow: auto;
      background: #151b17;
      color: #f5f1e9;
      border-radius: 16px;
      padding: 14px;
      font-size: 13px;
      line-height: 1.45;
    }
    details { margin-top: 10px; }
    summary { cursor: pointer; color: var(--accent); font-weight: 750; }
    .empty, .error {
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 18px;
      color: var(--muted);
      background: rgba(255,255,255,.55);
    }
    .error { color: var(--danger); border-color: rgba(180, 83, 9, .35); background: rgba(255, 247, 237, .8); }
    @media (max-width: 860px) {
      .grid { grid-template-columns: 1fr; }
      .panel { position: static; }
    }
  </style>
</head>
<body>
  <main>
    <h1>Debug trace</h1>
    <p>Показывает путь решения: classification → restricted check → KB/price lookup → policy → final answer.</p>
    <div class="grid">
      <section class="panel">
        <label for="company">company_id</label>
        <input id="company" value="rosh_demo" placeholder="rosh_demo" />

        <label for="message">Сообщение</label>
        <textarea id="message" placeholder="Сообщение">сколько стоит чистка лица</textarea>

        <div class="actions">
          <button id="run">Проверить</button>
          <button class="secondary" id="copy" type="button">Копировать JSON</button>
        </div>

        <div class="hint">
          Это внутренний инструмент. Он не пишет лиды, не меняет реальные сессии
          и нужен для разбора спорных ответов перед добавлением кейса в evals.
        </div>

        <div class="samples">
          <button class="sample" type="button">сколько стоит чистка лица</button>
          <button class="sample" type="button">чистка лица</button>
          <button class="sample" type="button">а что это?</button>
          <button class="sample" type="button">есть ботокс?</button>
          <button class="sample" type="button">у меня воспаление что делать</button>
          <button class="sample" type="button">хочу оставить телефон</button>
          <button class="sample" type="button">ps5 или xbox</button>
        </div>
      </section>

      <section id="output" class="output">
        <div class="empty">Введите сообщение и нажмите «Проверить».</div>
      </section>
    </div>
  </main>
  <script>
    const token = new URLSearchParams(location.search).get("token") || "";
    let lastPayload = null;

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function asJson(value) {
      return JSON.stringify(value ?? {}, null, 2);
    }

    function renderValue(value) {
      if (value === null || value === undefined || value === "") return "—";
      if (typeof value === "object") return `<pre>${escapeHtml(asJson(value))}</pre>`;
      return escapeHtml(value);
    }

    function renderStep(step) {
      const result = step.result || {};
      const rows = Object.entries(result)
        .filter(([key]) => !["local", "final"].includes(key))
        .map(([key, value]) => `<div>${escapeHtml(key)}</div><div>${renderValue(value)}</div>`)
        .join("");
      const localFinal = result.local || result.final
        ? `<details open>
             <summary>classification details</summary>
             <pre>${escapeHtml(asJson({local: result.local, final: result.final}))}</pre>
           </details>`
        : "";
      return `
        <article class="card">
          <h3>
            <span>${escapeHtml(step.step)}</span>
            <span class="duration">${step.duration_ms ? `${step.duration_ms} ms` : ""}</span>
          </h3>
          ${rows ? `<div class="kv">${rows}</div>` : ""}
          ${localFinal}
        </article>
      `;
    }

    function renderPayload(payload) {
      const steps = Array.isArray(payload.steps) ? payload.steps : [];
      const quickActions = Array.isArray(payload.quick_actions)
        ? payload.quick_actions.map((item) => item.label || item.value).filter(Boolean).join(", ")
        : "";
      return `
        <section class="summary card">
          <h2>Итог</h2>
          <div class="badges">
            <span class="badge strong">${escapeHtml(payload.final_action || "unknown")}</span>
            <span class="badge">${escapeHtml(payload.company_id || "")}</span>
            <span class="badge">${escapeHtml(payload.total_time_ms || 0)} ms</span>
            ${payload.lead_preview ? '<span class="badge">lead preview</span>' : ""}
          </div>
          <div class="answer">${escapeHtml(payload.final_answer || "Нет ответа")}</div>
          ${quickActions ? `<p class="hint">Quick actions: ${escapeHtml(quickActions)}</p>` : ""}
        </section>
        <section class="steps">
          ${steps.map(renderStep).join("")}
        </section>
        <details>
          <summary>raw JSON</summary>
          <pre>${escapeHtml(asJson(payload))}</pre>
        </details>
      `;
    }

    document.getElementById("run").onclick = async () => {
      const output = document.getElementById("output");
      output.innerHTML = '<div class="empty">Загрузка...</div>';
      try {
        const response = await fetch(`/api/debug/trace?token=${encodeURIComponent(token)}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            company_id: document.getElementById("company").value,
            message: document.getElementById("message").value
          })
        });
        const payload = await response.json();
        lastPayload = payload;
        if (!response.ok) {
          output.innerHTML = `<div class="error">${escapeHtml(payload.detail || response.statusText)}</div>`;
          return;
        }
        output.innerHTML = renderPayload(payload);
      } catch (error) {
        output.innerHTML = `<div class="error">${escapeHtml(error.message || error)}</div>`;
      }
    };

    document.getElementById("copy").onclick = async () => {
      if (!lastPayload) return;
      await navigator.clipboard.writeText(asJson(lastPayload));
    };

    document.querySelectorAll(".sample").forEach((button) => {
      button.onclick = () => {
        document.getElementById("message").value = button.textContent || "";
      };
    });
  </script>
</body>
</html>
"""


@router.get("/debug/rag", response_class=HTMLResponse)
async def rag_debug_page(request: Request) -> str:
    verify_operator_token(request, None)
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>RAG debug search</title>
  <style>
    :root {
      --bg: #f7f4ee;
      --card: #fffdf9;
      --ink: #202821;
      --muted: #68746d;
      --line: #e2dbd0;
      --accent: #1f7a5c;
      --danger: #b45309;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top left, rgba(31, 122, 92, .12), transparent 34rem), var(--bg);
      color: var(--ink);
    }
    main { max-width: 1060px; margin: 0 auto; padding: 32px 20px 48px; }
    h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: -.04em; }
    p { margin: 0; color: var(--muted); }
    .panel, .match, .meta {
      background: rgba(255, 253, 249, .92);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: 0 14px 36px rgba(45, 95, 79, .08);
    }
    .panel { margin-top: 22px; padding: 18px; }
    label { display: block; margin: 0 0 8px; font-weight: 800; }
    textarea, input, button { font: inherit; }
    textarea, input {
      width: 100%;
      border: 1px solid #d8d0c4;
      border-radius: 14px;
      padding: 12px 14px;
      background: white;
      color: var(--ink);
    }
    textarea { min-height: 86px; resize: vertical; }
    .row { display: grid; grid-template-columns: 1fr 120px auto; gap: 12px; align-items: end; }
    button {
      border: 0;
      border-radius: 14px;
      background: var(--accent);
      color: white;
      font-weight: 850;
      padding: 12px 18px;
      cursor: pointer;
    }
    .samples { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .sample {
      background: #eef6f2;
      color: var(--accent);
      border: 1px solid #d7e8df;
      padding: 8px 10px;
      font-size: 13px;
    }
    #output { display: grid; gap: 12px; margin-top: 18px; }
    .meta { padding: 14px 16px; color: var(--muted); font-size: 14px; }
    .match { padding: 16px; }
    .match h2 { margin: 0 0 8px; font-size: 18px; }
    .match a { color: var(--accent); overflow-wrap: anywhere; }
    .badge {
      display: inline-flex;
      margin: 0 8px 10px 0;
      border-radius: 999px;
      padding: 4px 9px;
      background: #eef6f2;
      color: var(--accent);
      font-size: 13px;
      font-weight: 750;
    }
    blockquote {
      margin: 10px 0 0;
      padding: 12px 14px;
      border-left: 4px solid var(--accent);
      background: white;
      border-radius: 10px;
      line-height: 1.5;
    }
    .empty, .error {
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 18px;
      color: var(--muted);
      background: rgba(255,255,255,.6);
    }
    .error { color: var(--danger); border-color: rgba(180, 83, 9, .35); }
    @media (max-width: 760px) { .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>RAG debug search</h1>
    <p>Read-only lexical поиск по staged article chunks. Это проверка источников до pgvector/embeddings.</p>

    <section class="panel">
      <div class="row">
        <div>
          <label for="query">Вопрос</label>
          <textarea id="query">как проходит кольпоскопия</textarea>
        </div>
        <div>
          <label for="topK">top_k</label>
          <input id="topK" type="number" min="1" max="20" value="5" />
        </div>
        <button id="run">Искать</button>
      </div>
      <div class="samples">
        <button class="sample" type="button">как проходит кольпоскопия</button>
        <button class="sample" type="button">что нельзя после лазерной шлифовки</button>
        <button class="sample" type="button">ботулинотерапия при мигрени</button>
        <button class="sample" type="button">подбор контрацептивов обследования</button>
        <button class="sample" type="button">филлеры в гинекологии</button>
      </div>
    </section>

    <section id="output">
      <div class="empty">Введите вопрос и нажмите «Искать».</div>
    </section>
  </main>

  <script>
    const token = new URLSearchParams(location.search).get("token") || "";

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function render(payload) {
      const matches = Array.isArray(payload.matches) ? payload.matches : [];
      const meta = `
        <div class="meta">
          chunks: ${escapeHtml(payload.total_chunks || 0)}
          · tokens: ${escapeHtml((payload.tokens || []).join(", ") || "—")}
          · file: ${escapeHtml(payload.chunks_file || "—")}
        </div>
      `;
      if (!matches.length) {
        return meta + '<div class="empty">Совпадений нет.</div>';
      }
      return meta + matches.map((match, index) => `
        <article class="match">
          <h2>${index + 1}. ${escapeHtml(match.title || "Без заголовка")}</h2>
          <span class="badge">score ${escapeHtml(match.score)}</span>
          <span class="badge">chunk ${escapeHtml(match.chunk_index)}</span>
          <span class="badge">${escapeHtml(match.source_type || "article")}</span>
          <div><a href="${escapeHtml(match.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(match.url || "—")}</a></div>
          <blockquote>${escapeHtml(match.snippet || "")}</blockquote>
        </article>
      `).join("");
    }

    document.getElementById("run").onclick = async () => {
      const output = document.getElementById("output");
      output.innerHTML = '<div class="empty">Поиск...</div>';
      try {
        const response = await fetch(`/api/debug/rag-search?token=${encodeURIComponent(token)}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            query: document.getElementById("query").value,
            top_k: Number(document.getElementById("topK").value || 5)
          })
        });
        const payload = await response.json();
        if (!response.ok) {
          output.innerHTML = `<div class="error">${escapeHtml(payload.detail || response.statusText)}</div>`;
          return;
        }
        output.innerHTML = render(payload);
      } catch (error) {
        output.innerHTML = `<div class="error">${escapeHtml(error.message || error)}</div>`;
      }
    };

    document.querySelectorAll(".sample").forEach((button) => {
      button.onclick = () => { document.getElementById("query").value = button.textContent || ""; };
    });
  </script>
</body>
</html>
"""

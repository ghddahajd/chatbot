"""внутренние debug-инструменты для демонстрации decision trace."""

from __future__ import annotations

import time
import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import verify_operator_token
from ..debug_sandbox import Sandbox
from ..knowledge import normalize_text
from ..models import ChatMessageResponse
from ..policy import classify_and_extract
from ..policy.constants import DURATION_KEYWORDS, PRICE_KEYWORDS
from ..policy.extractors import contains_keyword
from ..policy.restricted import is_restricted_question
from ..preflight import run_preflight
from ..services.chat_service import ChatService
from ..services.rag_search import retrieve_article_context, search_rag_chunks
from .chat_utils import service_classifier_payload


router = APIRouter(tags=["debug"])


class DebugTraceRequest(BaseModel):
    company_id: str
    message: Optional[str] = None
    # несколько сообщений подряд — один диалог: видно, как бот ведёт разговор, а не только первый ответ
    messages: Optional[list[str]] = Field(default=None, min_length=1, max_length=10)


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    # чей корпус искать; пусто — клиент по умолчанию
    company_id: Optional[str] = None


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
    messages = [text.strip() for text in (payload.messages or [payload.message or ""])]
    if not messages or not all(messages):
        raise HTTPException(status_code=400, detail="Message is empty")

    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(payload.company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    # настоящий обработчик чата в песочнице: ответ тот же, что увидит человек, а заявки,
    # Telegram и аналитика только записываются
    sandbox = Sandbox(request)
    session_id: Optional[str] = None
    turns: list[dict[str, Any]] = []
    last_response: Optional[ChatMessageResponse] = None
    for text in messages:
        effects_before, decisions_before = len(sandbox.effects), len(sandbox.decisions)
        response = await ChatService(sandbox.request).handle_message(
            company_id=payload.company_id, session_id=session_id, message=text
        )
        if not isinstance(response, ChatMessageResponse):
            raise HTTPException(status_code=response.status_code, detail="Chat pipeline returned an error")
        session_id, last_response = response.session_id, response
        decision = sandbox.decisions[-1]["result"] if len(sandbox.decisions) > decisions_before else None
        turns.append(
            {
                "message": text,
                "answer": response.answer,
                "action": _enum_value(response.action),
                "status": _enum_value(response.status),
                # None — ход решили шаги записи до правил (выбор дня, номер в ожидании)
                "reason": _enum_value(decision.reason) if decision else None,
                "rule": decision.rule if decision else None,
                "rules_matched": decision.rules_matched if decision else [],
                "quick_actions": [action.model_dump() for action in response.quick_actions],
                "effects": sandbox.effects[effects_before:],
            }
        )

    message = messages[-1]
    last_decision = sandbox.decisions[-1] if sandbox.decisions and turns[-1]["reason"] is not None else None
    classification = last_decision["classification"] if last_decision else {}
    policy_result = last_decision["result"] if last_decision else None
    steps = _explain_steps(request, knowledge_base, message, classification, policy_result)
    steps.append(
        {
            "step": "llm_generation",
            "result": {
                "mode": "chat_service",
                "provider": request.app.state.settings.llm_provider,
                "model": request.app.state.settings.llm_model,
            },
        }
    )
    steps.append({"step": "validation", "result": {"passed": True, "note": "ответ прошёл те же проверки, что в чате"}})

    return {
        "company_id": payload.company_id,
        "message": message,
        "steps": steps,
        "final_action": turns[-1]["action"],
        "final_answer": last_response.answer,
        "lead_preview": any(effect["service"] == "lead" for effect in turns[-1]["effects"]),
        "quick_actions": turns[-1]["quick_actions"],
        "turns": turns,
        "total_time_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }


def _explain_steps(
    request: Request,
    knowledge_base: Any,
    message: str,
    classification: dict[str, Any],
    policy_result: Any,
) -> list[dict[str, Any]]:
    """пояснения к последнему ходу: что понял классификатор, что нашлось в прайсе и статьях, какое
    правило решило. На ответ не влияют — он уже посчитан настоящим обработчиком."""

    known_services = service_classifier_payload(request, knowledge_base, include_variants=True)
    local_classification = classify_and_extract(
        message, known_services, knowledge_base.company.city, knowledge_base.domain_profile
    )
    is_restricted, restricted_category = is_restricted_question(message, knowledge_base.domain_profile)
    service = knowledge_base.find_service_by_id(classification.get("service_id"))
    context = knowledge_base.get_service_context(service) if service else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else None

    normalized_message = normalize_text(message)
    price_requested = classification.get("intent") == "price_question" or contains_keyword(normalized_message, PRICE_KEYWORDS)
    rag_triggered = (
        classification.get("intent") == "faq_question"
        and not price_requested
        and not contains_keyword(normalized_message, DURATION_KEYWORDS)
    )
    rag_error = None
    article_matches: list[dict[str, Any]] = []
    if rag_triggered:
        corpus_path = knowledge_base.rag_corpus_path()
        if corpus_path is None:
            rag_error = f"no_corpus: rag.corpus={knowledge_base.rag_corpus}"
        else:
            try:
                article_matches = retrieve_article_context(
                    f"{service.name} {message}" if service is not None else message, path=corpus_path
                )
            except FileNotFoundError:
                rag_error = f"corpus_not_found: {corpus_path}"
            except (json.JSONDecodeError, ValueError) as error:
                rag_error = f"invalid_corpus: {type(error).__name__}"

    safe_context = policy_result.safe_context if policy_result is not None else {}
    candidate = safe_context.get("article_guidance_candidate")
    cosmetic_used = safe_context.get("question_type") == "cosmetic_article_guidance"
    mapping = safe_context.get("article_service_mapping")
    return [
        {"step": "classification", "result": {"local": local_classification, "final": classification}},
        {
            "step": "restricted_check",
            "result": {"is_restricted": is_restricted, "category": restricted_category, "domain_profile": knowledge_base.domain_profile},
        },
        {
            "step": "kb_lookup",
            "result": {
                "source": "services.json",
                "found": service is not None,
                "service_id": service.id if service else None,
                "service_name": service.name if service else None,
            },
        },
        {
            "step": "price_lookup",
            "result": {"source": "prices.json", "found": price is not None, "price_text": price.get("price_text") if price else None},
        },
        {
            "step": "rag_retrieval",
            "result": {
                "triggered": rag_triggered,
                "matches": article_matches,
                "error": rag_error,
                "used": safe_context.get("question_type") == "faq_question",
            },
        },
        {
            "step": "cosmetic_article_guidance",
            "result": {
                "used": cosmetic_used,
                "approved_mapping_found": bool(mapping),
                "excerpt_present": bool(isinstance(candidate, dict) and str(candidate.get("excerpt") or "").strip()),
                "llm_candidate_available": isinstance(candidate, dict),
                "fallback_template": str(candidate.get("fallback_message_to_user") or "") if isinstance(candidate, dict) else None,
                "mapping": mapping if isinstance(mapping, dict) else None,
                "matches": safe_context.get("article_context") if cosmetic_used else [],
            },
        },
        {
            "step": "policy_decision",
            "result": {
                "action": _enum_value(policy_result.action) if policy_result else None,
                "reason": _enum_value(policy_result.reason) if policy_result else None,
                "service_id": policy_result.service_id if policy_result else None,
                "confidence": policy_result.confidence if policy_result else None,
                "quick_actions": policy_result.quick_actions if policy_result else [],
                "safe_context_keys": sorted(safe_context.keys()),
                "rule": policy_result.rule if policy_result else None,
                "rules_matched": policy_result.rules_matched if policy_result else [],
            },
        },
    ]


@router.get("/api/debug/telegram-check")
async def debug_telegram_check(
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Side-effect-free проверка "жива ли доставка в Telegram" — getMe + getChatMember, оба
    read-only Bot API методы. Ручной прогон (например сразу после деплоя), не для
    автоматического опроса — см. TelegramBridgeService.health_check для деталей и почему это
    не часть /health."""

    verify_operator_token(request, x_operator_token)
    bridge = getattr(request.app.state, "telegram_bridge_service", None)
    if bridge is None:
        return {
            "enabled": False,
            "bot_token": {"status": "skip", "detail": "telegram_bridge_service не сконфигурирован"},
            "operators_group": {"status": "skip", "detail": "—"},
        }
    return await bridge.health_check()


@router.get("/api/debug/domain-check")
async def debug_domain_check(
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Side-effect-free проверка "каждый настроенный домен резолвится ровно в одного
    клиента" — переиспользует KnowledgeBaseResolver.build_domain_index() (тот же индекс,
    что и реальный автодетект по Origin в /api/widget/bootstrap), никаких сетевых вызовов.

    2026-08-29: идея пользователя после живого разбора истории с scratchwidgettestsite.
    vercel.app — быстрый взгляд "все ли домены на своих местах" вместо ручного curl/захода
    на каждый сайт по отдельности. localhost сознательно исключён из отчёта — у него
    заведомо несколько локальных тестовых клиентов (rosh_demo и т.д.), это старый, известный
    и не мешающий факт (localhost и так никогда не спутаешь с реальным доменом), а не то,
    что стоит показывать в этом отчёте как проблему."""

    verify_operator_token(request, x_operator_token)
    resolver = request.app.state.knowledge_base_resolver
    domain_index = resolver.build_domain_index()

    domains = []
    for domain, company_ids in sorted(domain_index.items()):
        if domain == "localhost":
            continue
        if len(company_ids) > 1:
            domains.append(
                {
                    "domain": domain,
                    "status": "error",
                    "detail": f"домен задублирован между: {', '.join(company_ids)}",
                }
            )
        else:
            domains.append({"domain": domain, "status": "ok", "company_id": company_ids[0]})
    return {"domains": domains}


@router.get("/api/debug/preflight")
async def debug_preflight(
    request: Request,
    network: bool = True,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Одна read-only проверка «всё ли на месте»: здоровье, клиенты и их данные, домены, сбор
    лидов, аналитика, диск, сессии, логирование и Telegram (getMe/getChatMember — оба
    read-only). Ничего не пишет и не шлёт клиентам, секреты не показывает. network=0 —
    без обращения к Telegram. Удобный запуск: backend/scripts/preflight.py."""

    verify_operator_token(request, x_operator_token)
    return await run_preflight(request.app, include_network=network)


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

    company_id = payload.company_id or request.app.state.settings.default_company_id
    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error
    corpus_path = knowledge_base.rag_corpus_path()
    if corpus_path is None:
        raise HTTPException(
            status_code=404, detail=f"У клиента {company_id} нет корпуса статей (rag.corpus={knowledge_base.rag_corpus})"
        )

    try:
        return search_rag_chunks(query=query, top_k=payload.top_k, path=corpus_path)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail=f"RAG chunks corpus not found for {company_id}. Expected: {corpus_path}",
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

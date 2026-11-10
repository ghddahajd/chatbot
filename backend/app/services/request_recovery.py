"""Повтор запроса восстанавливает результат, не запускает диалог повторно вслепую."""

import asyncio
import hashlib
import json
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ..models import ChatMessageResponse, PolicyAction, SessionStatus
from ..storage.errors import LeadStorageError
from ..storage.request_context import CURRENT_REQUEST, RequestOperation
from ..storage.request_receipts import RequestConflict

logger = logging.getLogger(__name__)


def fingerprint(payload):
    body = payload.model_dump(mode="json", exclude={"request_id", "retry"})
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def validate_company(request, payload):
    try:
        request.app.state.knowledge_base_resolver.get(payload.company_id, fallback=False)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown company") from None


def render_result(result):
    if result["state"] == "done":
        try:
            return ChatMessageResponse.model_validate(result["response"])
        except Exception:
            raise LeadStorageError("Сохранённый ответ недоступен") from None
    return JSONResponse(status_code=202, content=result)


async def request_status(request, payload):
    validate_company(request, payload)
    if payload.request_id is None:
        raise HTTPException(status_code=422, detail="request_id required")
    try:
        result = await request.app.state.request_receipts.status(
            payload.company_id, str(payload.request_id), fingerprint(payload))
        if result["state"] == "done":
            result = {**result, "response": render_result(result).model_dump(mode="json")}
        return result
    except RequestConflict:
        raise HTTPException(status_code=409, detail="request_id_conflict") from None
    except Exception as error:
        logger.warning("request status failed error=%s", type(error).__name__)
        raise LeadStorageError("Не удалось проверить отправку") from None


async def recoverable_message(request, payload, service):
    async def run():
        return await service.handle_message(company_id=payload.company_id, session_id=payload.session_id,
            message=payload.message, page=payload.page, booking_day=payload.booking_day)

    if payload.request_id is None:
        if payload.retry:
            raise HTTPException(status_code=422, detail="request_id required")
        return await run()
    validate_company(request, payload)
    receipts = request.app.state.request_receipts
    key = str(payload.request_id)
    digest = fingerprint(payload)
    try:
        result = await receipts.begin(payload.company_id, key, digest, retry=payload.retry)
    except RequestConflict:
        raise HTTPException(status_code=409, detail="request_id_conflict") from None
    except Exception as error:
        logger.warning("request begin failed error=%s", type(error).__name__)
        raise LeadStorageError("Не удалось сохранить отправку") from None
    if result["state"] != "started":
        return render_result(result)

    async def recovery_factory(lead, event_type):
        kb = request.app.state.knowledge_base_resolver.get(lead.company_id, fallback=False)
        session = await request.app.state.session_store.get(lead.session_id)
        return ChatMessageResponse(
            session_id=lead.session_id, status=session.status if session else SessionStatus.AI_ACTIVE,
            action=PolicyAction.ASK_CONTACT, lead_created=True,
            answer=service._lead_success_answer(lead, is_booking_request=event_type == "booking_created",
                                                knowledge_base=kb), quick_actions=[],
        ).model_dump(mode="json")

    operation = RequestOperation(receipts, payload.company_id, key, digest,
                                 result["claim_token"], recovery_factory)
    token = CURRENT_REQUEST.set(operation)
    try:
        response = await run()
        if isinstance(response, ChatMessageResponse):
            saved = await receipts.finish(payload.company_id, key, operation.claim_token,
                                            response.model_dump(mode="json"))
            if not saved:
                raise LeadStorageError("Результат отправки не подтверждён")
        else:
            await receipts.fail(payload.company_id, key, operation.claim_token)
        return response
    except BaseException as error:
        try:
            await asyncio.shield(receipts.fail(payload.company_id, key, operation.claim_token))
        except Exception as cleanup_error:
            logger.warning("request finalization failed error=%s", type(cleanup_error).__name__)
        if isinstance(error, Exception) and operation.committed_response is not None:
            logger.warning("request interrupted after lead commit error=%s", type(error).__name__)
            return ChatMessageResponse.model_validate(operation.committed_response)
        if isinstance(error, Exception) and not isinstance(error, HTTPException):
            logger.warning("request processing failed error=%s", type(error).__name__)
            raise LeadStorageError("Не удалось подтвердить отправку") from None
        raise
    finally:
        CURRENT_REQUEST.reset(token)

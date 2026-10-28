"""цены в ответах: строка «услуга — от N ₽» и цена под страницу, с которой человек пишет."""

from __future__ import annotations

from typing import Any, Optional

from ..knowledge import KnowledgeBase, is_consultation_only_service, normalize_text
from ..models import ArticleServiceMapEntry, PolicyAction, PolicyReason, PolicyResult

# больше четырёх строк в ответе на «сколько стоит?» читают как прайс, а не как подсказку
PAGE_PRICE_MAX_SERVICES = 4
BOOK_ACTION = {"label": "Записаться", "type": "message", "value": "Хочу записаться"}
ALL_PRICES_ACTION = {"label": "Все цены", "type": "message", "value": "Все услуги и цены"}
# кнопки направлений шлют фиксированный текст — бот узнаёт его сам, без нейросети
DIRECTION_PREFIX = "Цены: "
OTHER_DIRECTION = "Другое направление"
OTHER_DIRECTION_ACTION = {"label": OTHER_DIRECTION, "type": "message", "value": OTHER_DIRECTION}


def _amount(amount: float) -> str:
    return f"{int(amount):,}".replace(",", " ")


def _rubles(amount: float) -> str:
    return f"{_amount(amount)} ₽"


def _price_from_line(knowledge_base: KnowledgeBase, service, variant: dict[str, object] | None = None) -> str | None:
    """«Название — от N ₽» для обзора цен; без цены в прайсе — None."""

    if variant is not None:
        label = " ".join(str(variant.get("name") or "").split())
        price = variant.get("price_from")
        if not label or not isinstance(price, (int, float)) or price <= 0:
            return None
        return f"{label} — {_rubles(price)}"
    price_from, price_to = getattr(service, "price_from", None), getattr(service, "price_to", None)
    if not isinstance(price_from, (int, float)) or price_from <= 0:
        return None
    prefix = "" if price_to == price_from else "от "
    unit = " за единицу" if knowledge_base.price_unit_note(service) else ""
    return f"{' '.join(service.name.split())} — {prefix}{_rubles(price_from)}{unit}"


def _closing(service) -> str:
    # у консультации «схему подберёт врач на консультации» звучит по кругу
    if is_consultation_only_service(service):
        return "Записать вас?"
    return "Точную схему и стоимость подберёт врач на консультации. Записать вас?"


def _focus_prices(entry: ArticleServiceMapEntry, services: list[Any], catalog: list[Any]) -> Optional[tuple[Any, list[float]]]:
    markers = [marker.lower() for marker in entry.price_focus if marker.strip()]
    if not markers:
        return None
    # сначала в услугах страницы; не нашлось — по всему прайсу: статье про гинекологию нужна
    # «Консультация гинеколога», а она лежит в «Консультациях», не в «Гинекологии»
    for candidates in (services, catalog):
        owner, prices = None, []
        for service in candidates:
            for variant in service.variants or []:
                name, price = str(variant.get("name") or "").lower(), variant.get("price_from")
                if isinstance(price, (int, float)) and price > 0 and any(marker in name for marker in markers):
                    owner = owner or service
                    prices.append(price)
        if prices:
            return owner, prices
    return None


def _page_answer(message: str, service_id: Optional[str], actions: list[object], confidence: float) -> PolicyResult:
    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.PRICE_QUESTION,
        service_id=service_id,
        confidence=confidence,
        safe_context={"force_direct_answer": True, "question_type": "page_price", "message_to_user": message},
        quick_actions=actions,
    )


def page_price_result(knowledge_base: KnowledgeBase, page: Optional[str], *, confidence: float) -> Optional[PolicyResult]:
    """на общий вопрос о ценах — цена услуги той страницы, где сейчас человек: читатель статьи про PDRN
    видит цену PDRN, а не общий список. Страницы нет в связке — None, отвечает общий список."""

    entry = knowledge_base.page_entry(page)
    if entry is None:
        return None
    services = [service for service in map(knowledge_base.find_service_by_id, entry.service_ids) if service is not None]
    if not services:
        return None

    focus = _focus_prices(entry, services, knowledge_base.services)
    if focus is not None:
        service, prices = focus
        low, high = min(prices), max(prices)
        span = _rubles(low) if low == high else f"от {_amount(low)} до {_rubles(high)}"
        title = entry.price_focus_title or service.name
        return _page_answer(f"{title} — {span}. {_closing(service)}", service.id, [BOOK_ACTION, ALL_PRICES_ACTION], confidence)

    if len(services) == 1:
        service = services[0]
        price_entry = knowledge_base.find_price_by_service_id(service.id)
        price_text = service.price_range_text or (price_entry.price_text if price_entry else "")
        if not price_text:
            return None
        unit = " за единицу" if knowledge_base.price_unit_note(service) else ""
        actions: list[object] = [BOOK_ACTION]
        if len(service.variants or []) > 1:
            actions.append({"label": "Все варианты и цены", "type": "message", "value": f"Покажи все варианты и цены: {service.name}"})
        actions.append(ALL_PRICES_ACTION)
        name = " ".join(service.name.split())
        return _page_answer(f"{name} — {price_text}{unit}. {_closing(service)}", service.id, actions, confidence)

    lines = [line for service in services[:PAGE_PRICE_MAX_SERVICES] if (line := _price_from_line(knowledge_base, service))]
    if not lines:
        return None
    # перечисляем, что есть, без «помогает»: страницы-проблемы часто медицинские
    message = "\n".join(
        ["По этой теме в клинике есть:", *(f"• {line}" for line in lines), "Что подойдёт именно вам, скажет врач на консультации. Записать вас?"]
    )
    return _page_answer(message, None, [BOOK_ACTION, ALL_PRICES_ACTION], confidence)


def _direction_action(title: str) -> dict[str, str]:
    return {"label": title, "type": "message", "value": f"{DIRECTION_PREFIX}{title.lower()}"}


def overview_actions(knowledge_base: KnowledgeBase, show_all: bool) -> list[object]:
    """кнопки под общим списком цен: есть направления — они вместо «Позвать менеджера», чтобы не
    перегружать; полный прайс — без них."""

    directions = [] if show_all else knowledge_base.price_directions()
    if directions:
        return [BOOK_ACTION, *(_direction_action(title) for title, _ in directions), ALL_PRICES_ACTION]
    actions: list[object] = [BOOK_ACTION]
    if not show_all:
        actions.append({"label": "Все услуги и цены", "type": "message", "value": "Все услуги и цены"})
    actions.append("Позвать менеджера")
    return actions


def price_direction_result(knowledge_base: KnowledgeBase, normalized_message: str, *, footer: str) -> Optional[PolicyResult]:
    """нажали направление — его услуги с ценой «от»; «Другое направление» — снова кнопки направлений."""

    directions = knowledge_base.price_directions()
    if not directions:
        return None
    if normalized_message == normalize_text(OTHER_DIRECTION):
        actions = [*(_direction_action(title) for title, _ in directions), ALL_PRICES_ACTION]
        return _direction_answer("Какое направление интересует?", actions)
    for title, items in directions:
        if normalized_message != normalize_text(f"{DIRECTION_PREFIX}{title}"):
            continue
        lines = [line for service, variant in items if (line := _price_from_line(knowledge_base, service, variant))]
        message = "\n".join([f"{title} — цены:", *(f"• {line}" for line in lines), footer])
        return _direction_answer(message, [BOOK_ACTION, OTHER_DIRECTION_ACTION])
    return None


def _direction_answer(message: str, actions: list[object]) -> PolicyResult:
    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.PRICE_QUESTION,
        confidence=0.95,
        safe_context={"force_direct_answer": True, "question_type": "price_direction", "message_to_user": message},
        quick_actions=actions,
    )

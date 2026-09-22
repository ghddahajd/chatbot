"""куда слать карточки Telegram для каждого клиента (шаг 4 дорожной карты, 2026-09-23).

Один бот, у каждого клиента своя супергруппа операторов. Три режима:

- legacy — карта не задана (TELEGRAM_CLIENT_GROUPS пусто): всё как раньше, одна общая группа
  TELEGRAM_OPERATORS_GROUP_ID и тема «Клиенты» TELEGRAM_CLIENTS_TOPIC_ID для любого клиента;
- map — карта задана: у клиента из карты — его группа и тема; клиент не из карты не получает
  НИЧЕГО (лид сохраняется, в логе предупреждение) — чужая группа исключена;
- invalid — карта задана с ошибкой: новые карточки не уходят никому, preflight и
  telegram-check показывают ошибку. Бот при этом продолжает работать.

Формат карты — JSON одной строкой в .env:
    TELEGRAM_CLIENT_GROUPS={"rosh_import_demo": {"group": "-100123", "topic": "5"}, "rosh_test": {"group": "-100123"}}

Плавный переход: сначала карта с ТОЙ ЖЕ группой, что сейчас общая (карточки идут туда же, но уже
по карте), проверка telegram-check, и только потом группа нового клиента.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

LEGACY = "legacy"
MAP = "map"
INVALID = "invalid"
_COMPANY_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_GROUP_ID = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class TelegramTarget:
    group_id: str
    clients_topic_id: str = ""


def _parse_map(raw: str) -> tuple[dict[str, TelegramTarget], str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        return {}, f"TELEGRAM_CLIENT_GROUPS — не JSON: {error.msg} (позиция {error.pos})"
    if not isinstance(payload, dict) or not payload:
        return {}, "TELEGRAM_CLIENT_GROUPS — ожидается непустой объект {клиент: {group, topic}}"
    targets: dict[str, TelegramTarget] = {}
    for company_id, entry in payload.items():
        if not isinstance(company_id, str) or not _COMPANY_ID.match(company_id):
            return {}, f"TELEGRAM_CLIENT_GROUPS — странный id клиента: {company_id!r}"
        if not isinstance(entry, dict):
            return {}, f"TELEGRAM_CLIENT_GROUPS — у {company_id} ожидается объект с полем group"
        group = str(entry.get("group") or "").strip()
        topic = str(entry.get("topic") or "").strip()
        if not _GROUP_ID.match(group):
            return {}, f"TELEGRAM_CLIENT_GROUPS — у {company_id} нет числового group"
        if topic and not topic.isdigit():
            return {}, f"TELEGRAM_CLIENT_GROUPS — у {company_id} topic должен быть числом"
        targets[company_id] = TelegramTarget(group_id=group, clients_topic_id=topic)
    return targets, None


class TelegramRouting:
    def __init__(self, *, legacy_group_id: str = "", legacy_clients_topic_id: str = "", client_groups_raw: str = "") -> None:
        self.legacy_group_id = str(legacy_group_id or "").strip()
        self.legacy_target = (
            TelegramTarget(group_id=self.legacy_group_id, clients_topic_id=str(legacy_clients_topic_id or "").strip())
            if self.legacy_group_id
            else None
        )
        raw = str(client_groups_raw or "").strip()
        self.targets: dict[str, TelegramTarget] = {}
        self.error: str | None = None
        if not raw:
            self.mode = LEGACY
        else:
            self.targets, self.error = _parse_map(raw)
            self.mode = INVALID if self.error else MAP

    @property
    def configured(self) -> bool:
        """есть хотя бы одна группа, куда вообще можно что-то слать или откуда читать."""

        return bool(self.legacy_target or self.targets)

    def target_for(self, company_id: str | None) -> TelegramTarget | None:
        """куда слать НОВУЮ карточку клиента. None — никуда (клиент не из карты или карта с ошибкой)."""

        if self.mode == LEGACY:
            return self.legacy_target
        if self.mode == MAP and company_id:
            return self.targets.get(company_id)
        return None

    def groups(self) -> dict[str, list[str]]:
        """группа → клиенты (для проверок). В legacy — одна общая группа «для всех»."""

        if self.mode == LEGACY:
            return {self.legacy_group_id: ["*"]} if self.legacy_group_id else {}
        grouped: dict[str, list[str]] = {}
        for company_id, target in sorted(self.targets.items()):
            grouped.setdefault(target.group_id, []).append(company_id)
        return grouped

    def is_known_group(self, chat_id: str | None) -> bool:
        """сообщения и кнопки принимаем только из своих групп; старая общая группа остаётся «своей»
        всегда — в ней могут висеть темы, начатые до включения карты."""

        if not chat_id:
            return False
        return chat_id == self.legacy_group_id or any(target.group_id == chat_id for target in self.targets.values())

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "groups": len(self.groups()), "companies": sorted(self.targets), "error": self.error}

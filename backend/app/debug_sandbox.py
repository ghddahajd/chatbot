"""песочница для отладки: сообщение проходит через настоящий обработчик чата (те же правила, LLM и
данные клиента), а всё, что уходит наружу — заявки, Telegram, вебхуки, аналитика, — только
записывается. Иначе отладка показывала бы свою копию логики, а не то, что увидит человек."""

from __future__ import annotations

from typing import Any

from .sessions import SessionStore


class _Recorder:
    """внешняя служба-заглушка: любой await-вызов записывается и возвращает заранее заданный ответ."""

    def __init__(self, name: str, log: list[dict[str, Any]], returns: dict[str, Any] | None = None, **attrs: Any) -> None:
        self._name = name
        self._log = log
        self._returns = returns or {}
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, method: str):
        if method.startswith("__"):
            raise AttributeError(method)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._log.append({"service": self._name, "call": method, **_describe(args, kwargs)})
            return self._returns.get(method)

        return call


def _describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """что именно ушло бы наружу — без телефона и имени: отладку смотрят и по живым фразам."""

    details: dict[str, Any] = {}
    lead = next((arg for arg in args if hasattr(arg, "model_dump")), None)
    if lead is not None:
        dumped = lead.model_dump(mode="json")
        details["lead"] = {
            key: dumped.get(key)
            for key in ("reason", "service_id", "needs_operator", "preferred_time", "lead_trigger")
            if key in dumped
        }
        details["lead"]["phone_given"] = bool(dumped.get("phone"))
    for key in ("reason", "event_type", "is_lead"):
        if key in kwargs:
            details[key] = kwargs[key]
    return details


class _SandboxState:
    def __init__(self, real_state: Any, overrides: dict[str, Any]) -> None:
        object.__setattr__(self, "_real", real_state)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_real"), name)


class _SandboxApp:
    def __init__(self, real_app: Any, state: _SandboxState) -> None:
        self._real_app = real_app
        self.state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_app, name)


class _SandboxRequest:
    def __init__(self, real_request: Any, app: _SandboxApp) -> None:
        self._real_request = real_request
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_request, name)


class Sandbox:
    """один прогон отладки: своя пустая память сессий, внешние действия — в self.effects,
    решения правил по каждому ходу — в self.decisions."""

    def __init__(self, request: Any) -> None:
        self.effects: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        real_state = request.app.state
        real_analyzer = real_state.policy_analyzer

        def analyzer(message, session, knowledge_base, classification):
            result = real_analyzer(message, session, knowledge_base, classification)
            self.decisions.append({"message": message, "classification": dict(classification), "result": result})
            return result

        overrides = {
            "session_store": SessionStore(),
            "policy_analyzer": analyzer,
            "lead_service": _Recorder("lead", self.effects),
            "analytics_service": _Recorder("analytics", self.effects),
            "delivery_service": _Recorder("webhook", self.effects),
            # «доставлено»: иначе передача администратору пошла бы по ветке «Telegram недоступен»
            "telegram_bridge_service": _Recorder(
                "telegram", self.effects, returns={"post_operator_queue_card": True}, enabled=True
            ),
        }
        self.request = _SandboxRequest(request, _SandboxApp(request.app, _SandboxState(real_state, overrides)))

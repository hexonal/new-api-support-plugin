import asyncio
import importlib
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest


@dataclass
class StubPlatformConfig:
    enabled: bool = True
    token: str = ""
    extra: dict = field(default_factory=dict)


class StubPlatform(str):
    _members = {}

    def __new__(cls, value):
        normalized = str(value).lower()
        if normalized in cls._members:
            return cls._members[normalized]
        obj = str.__new__(cls, normalized)
        obj.value = normalized
        cls._members[normalized] = obj
        return obj


class StubBasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._message_handler = None
        self._running = False

    def _mark_connected(self):
        self._running = True

    def _mark_disconnected(self):
        self._running = False

    def _set_fatal_error(self, code, message, *, retryable):
        self._fatal = (code, message, retryable)
        self._running = False

    def set_message_handler(self, handler):
        self._message_handler = handler

    def build_source(
        self,
        chat_id,
        chat_name=None,
        chat_type="dm",
        user_id=None,
        user_name=None,
        thread_id=None,
        **kwargs,
    ):
        return types.SimpleNamespace(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id is not None else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id is not None else None,
        )


@dataclass
class StubMessageEvent:
    text: str
    message_type: object
    source: object
    raw_message: object = None
    message_id: Optional[str] = None
    auto_skill: object = None
    channel_prompt: Optional[str] = None


@dataclass
class StubSendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: object = None


class StubMessageType:
    TEXT = "text"


class StubWeb:
    class Response:
        def __init__(self, data, status=200):
            self.status = status
            self.text = json.dumps(data, ensure_ascii=False)

    @staticmethod
    def json_response(data, status=200):
        return StubWeb.Response(data, status=status)


def install_gateway_stubs(monkeypatch):
    gateway_mod = types.ModuleType("gateway")
    gateway_config_mod = types.ModuleType("gateway.config")
    gateway_config_mod.Platform = StubPlatform
    gateway_config_mod.PlatformConfig = StubPlatformConfig

    gateway_platforms_mod = types.ModuleType("gateway.platforms")
    gateway_platforms_base_mod = types.ModuleType("gateway.platforms.base")
    gateway_platforms_base_mod.BasePlatformAdapter = StubBasePlatformAdapter
    gateway_platforms_base_mod.MessageEvent = StubMessageEvent
    gateway_platforms_base_mod.MessageType = StubMessageType
    gateway_platforms_base_mod.SendResult = StubSendResult

    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms", gateway_platforms_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gateway_platforms_base_mod)


@pytest.fixture()
def adapter_module(monkeypatch):
    install_gateway_stubs(monkeypatch)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.modules.pop("new_api_support_platform.adapter", None)
    module = importlib.import_module("new_api_support_platform.adapter")
    monkeypatch.setattr(module, "web", StubWeb)
    yield module
    try:
        sys.path.remove(str(Path(__file__).resolve().parents[1]))
    except ValueError:
        pass


_DEFAULT_HEADERS = object()


def make_request(adapter_module, payload, *, token="secret", method="POST", headers=_DEFAULT_HEADERS):
    class Request:
        def __init__(self):
            self.method = method
            self.headers = {"Authorization": f"Bearer {token}"} if headers is _DEFAULT_HEADERS else headers
            self.content_length = len(json.dumps(payload).encode("utf-8"))

        async def json(self):
            return payload

    return Request()


async def _test_rejects_missing_bearer_token(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "require_token": True})
    )

    response = await adapter.handle_chat_request(
        make_request(adapter_module, {"session_id": "web_1", "message": "hi"}, headers={})
    )

    assert response.status == 401
    assert json.loads(response.text)["error"] == "unauthorized"


def test_rejects_missing_bearer_token(adapter_module):
    asyncio.run(_test_rejects_missing_bearer_token(adapter_module))


async def _test_rejects_unallowed_source(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(
            extra={
                "token": "secret",
                "allowed_sources": ["new-api-web"],
            }
        )
    )

    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {"session_id": "web_1", "message": "hi", "source": "other"},
        )
    )

    assert response.status == 403
    assert json.loads(response.text)["error"] == "source_not_allowed"


def test_rejects_unallowed_source(adapter_module):
    asyncio.run(_test_rejects_unallowed_source(adapter_module))


async def _test_builds_message_event_and_returns_agent_reply(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(
            extra={
                "token": "secret",
                "request_timeout_seconds": 5,
                "allowed_sources": ["new-api-web"],
            }
        )
    )
    captured = {}

    async def handler(event):
        captured["event"] = event
        return "请把 request_id 发我，我来查。"

    adapter.set_message_handler(handler)

    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {
                "session_id": "web_abc",
                "message": "接口 403 怎么办？",
                "source": "new-api-web",
                "user_id": 123,
                "role": 1,
                "context": {
                    "page_url": "https://new-api.example.com/contact",
                    "path": "/contact",
                    "title": "联系我们",
                    "client_ip": "203.0.113.10",
                },
            },
        )
    )

    body = json.loads(response.text)
    assert response.status == 200
    assert body == {"session_id": "web_abc", "reply": "请把 request_id 发我，我来查。"}
    event = captured["event"]
    assert event.text == "接口 403 怎么办？"
    assert event.auto_skill is None
    assert event.source.chat_id == "new-api-web:user:123:session:web_abc"
    assert event.source.thread_id is None
    assert event.source.user_id == "new-api-web:123"
    assert event.channel_prompt is None
    assert event.raw_message["context"]["page_url"] == "https://new-api.example.com/contact"


def test_builds_message_event_and_returns_agent_reply(adapter_module):
    asyncio.run(_test_builds_message_event_and_returns_agent_reply(adapter_module))


async def _test_returns_handler_reply_verbatim(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(
            extra={
                "token": "secret",
                "allowed_sources": ["new-api-web"],
            }
        )
    )

    async def handler(_event):
        return "raw handler reply"

    adapter.set_message_handler(handler)

    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {
                "session_id": "web_abc",
                "message": "接口 403",
                "source": "new-api-web",
                "user_id": "verbatim-user",
            },
        )
    )

    assert json.loads(response.text)["reply"] == "raw handler reply"


def test_returns_handler_reply_verbatim(adapter_module):
    asyncio.run(_test_returns_handler_reply_verbatim(adapter_module))


async def _test_rejects_missing_user_id_by_default(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "allowed_sources": ["new-api-web"]})
    )

    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {"session_id": "web_abc", "message": "hi", "source": "new-api-web"},
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error"] == "missing_user_id"


def test_rejects_missing_user_id_by_default(adapter_module):
    asyncio.run(_test_rejects_missing_user_id_by_default(adapter_module))


async def _test_accepts_alternate_user_id_fields(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "allowed_sources": ["new-api-web"]})
    )
    captured = {}

    async def handler(event):
        captured["event"] = event
        return "ok"

    adapter.set_message_handler(handler)
    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {
                "session_id": "web_abc",
                "message": "hi",
                "source": "new-api-web",
                "visitor_id": "visitor 42",
            },
        )
    )

    assert response.status == 200
    assert captured["event"].source.chat_id == "new-api-web:user:visitor_42:session:web_abc"
    assert captured["event"].source.user_id == "new-api-web:visitor_42"


def test_accepts_alternate_user_id_fields(adapter_module):
    asyncio.run(_test_accepts_alternate_user_id_fields(adapter_module))


async def _test_same_session_id_is_isolated_by_user_id(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "allowed_sources": ["new-api-web"]})
    )
    captured = []

    async def handler(event):
        captured.append(event)
        return "ok"

    adapter.set_message_handler(handler)
    for user_id in ("user-a", "user-b"):
        response = await adapter.handle_chat_request(
            make_request(
                adapter_module,
                {
                    "session_id": "web_shared",
                    "message": f"hi from {user_id}",
                    "source": "new-api-web",
                    "user_id": user_id,
                },
            )
        )
        assert response.status == 200

    assert captured[0].source.chat_id == "new-api-web:user:user-a:session:web_shared"
    assert captured[1].source.chat_id == "new-api-web:user:user-b:session:web_shared"
    assert captured[0].source.chat_id != captured[1].source.chat_id


def test_same_session_id_is_isolated_by_user_id(adapter_module):
    asyncio.run(_test_same_session_id_is_isolated_by_user_id(adapter_module))


async def _test_same_conversation_requests_are_serialized(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(
            extra={
                "token": "secret",
                "allowed_sources": ["new-api-web"],
                "request_timeout_seconds": 5,
            }
        )
    )
    order = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(event):
        order.append(("start", event.text))
        if event.text == "first":
            first_entered.set()
            await release_first.wait()
        order.append(("end", event.text))
        return event.text

    adapter.set_message_handler(handler)
    first = asyncio.create_task(
        adapter.handle_chat_request(
            make_request(
                adapter_module,
                {
                    "session_id": "web_serial",
                    "message": "first",
                    "source": "new-api-web",
                    "user_id": "serial-user",
                },
            )
        )
    )
    await first_entered.wait()
    second = asyncio.create_task(
        adapter.handle_chat_request(
            make_request(
                adapter_module,
                {
                    "session_id": "web_serial",
                    "message": "second",
                    "source": "new-api-web",
                    "user_id": "serial-user",
                },
            )
        )
    )
    await asyncio.sleep(0.05)
    assert order == [("start", "first")]
    release_first.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status == 200
    assert second_response.status == 200
    assert order == [
        ("start", "first"),
        ("end", "first"),
        ("start", "second"),
        ("end", "second"),
    ]


def test_same_conversation_requests_are_serialized(adapter_module):
    asyncio.run(_test_same_conversation_requests_are_serialized(adapter_module))


async def _test_send_collects_fallback_reply(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(StubPlatformConfig(extra={"token": "secret"}))
    pending = asyncio.get_running_loop().create_future()
    adapter._pending_http_replies["web_1"] = pending

    result = await adapter.send("web_1", "fallback reply")

    assert result.success is True
    assert pending.result() == "fallback reply"


def test_send_collects_fallback_reply(adapter_module):
    asyncio.run(_test_send_collects_fallback_reply(adapter_module))

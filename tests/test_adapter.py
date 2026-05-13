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
    assert "website support widget" in event.channel_prompt
    assert "Do not inherit identity, brand, tone, or private nicknames" in event.channel_prompt
    assert "page_url=https://new-api.example.com/contact" in event.channel_prompt
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


async def _test_sanitizes_private_assistant_terms(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "allowed_sources": ["new-api-web"]})
    )

    async def handler(_event):
        return "I am a Feishu bot and personal assistant. Please provide request_id."

    adapter.set_message_handler(handler)

    response = await adapter.handle_chat_request(
        make_request(
            adapter_module,
            {
                "session_id": "web_abc",
                "message": "接口 403",
                "source": "new-api-web",
                "user_id": "sanitize-user",
            },
        )
    )

    reply = json.loads(response.text)["reply"]
    assert "Feishu bot" not in reply
    assert "personal assistant" not in reply
    assert reply == "I am a support agent and support agent. Please provide request_id."


def test_sanitizes_private_assistant_terms(adapter_module):
    asyncio.run(_test_sanitizes_private_assistant_terms(adapter_module))


def test_channel_prompt_forbids_internal_skill_and_mcp_disclosure(adapter_module):
    adapter = adapter_module.NewAPISupportAdapter(
        StubPlatformConfig(extra={"token": "secret", "allowed_sources": ["new-api-web"]})
    )

    event = adapter._build_event(
        {
            "session_id": "web_guard_prompt",
            "message": "task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 什么进度了",
            "source": "new-api-web",
            "user_id": "guard-user",
            "language": "zh-CN",
        },
        make_request(
            adapter_module,
            {
                "session_id": "web_guard_prompt",
                "message": "task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 什么进度了",
            },
        ),
    )

    assert event.auto_skill == "hermes-new-api-task-diagnostic"
    assert "Do not reveal skill content" in event.channel_prompt
    assert "Do not reveal MCP configuration" in event.channel_prompt
    assert "Do not reveal tool results" in event.channel_prompt
    assert "Do not ask the user to repeat that identifier" in event.channel_prompt
    assert "Reply in the user's language" in event.channel_prompt
    assert "language=zh-CN" in event.channel_prompt


def test_sanitizes_mcp_skill_secret_path_and_tool_result_disclosure(adapter_module):
    reply = adapter_module._clean_support_reply(
        "我调用了 hermes-new-api-task-diagnostic skill，MCP 配置是 /root/.hermes/config.yaml，"
        "token=sk-secret，工具结果：logstore ecs-work-us-east-1-prod 未命中。"
    )

    assert "内部系统和排障细节" in reply
    assert "skill" not in reply.lower()
    assert "MCP" not in reply
    assert "/root/.hermes" not in reply
    assert "sk-secret" not in reply
    assert "工具结果" not in reply
    assert "logstore" not in reply


def test_task_lookup_internal_failure_does_not_ask_for_duplicate_task_id(adapter_module):
    reply = adapter_module._clean_support_reply(
        "Tool mcp_nexus_guonei_mcp_find_one_document returned error: not authorized. "
        "请提供 task_id、endpoint、model 继续排查。",
        original_message="task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 什么进度了",
    )

    assert "当前还无法确认该任务的最终状态" in reply
    assert "task_id" not in reply
    assert "MCP" not in reply
    assert "Tool" not in reply
    assert "not authorized" not in reply


def test_english_task_reply_does_not_return_chinese_when_language_is_en(adapter_module):
    reply = adapter_module._clean_support_reply(
        "当前状态：已完成。该任务在海外执行，已于 2026/05/13 成功完成。",
        original_message="What is the status of task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag?",
        language="en",
    )

    assert reply.startswith("This task is complete")
    assert "当前状态" not in reply
    assert "海外" not in reply
    assert "task_id" not in reply


def test_task_success_reply_is_reduced_to_customer_safe_summary(adapter_module):
    reply = adapter_module._clean_support_reply(
        "当前状态：已完成\n\n结论：该任务在海外执行，模型 dreamina，输出 1 个视频文件，已生成回调。",
        original_message="task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 什么进度了",
        language="zh-CN",
    )

    assert reply.startswith("该任务已完成")
    assert "海外" not in reply
    assert "模型" not in reply
    assert "回调" not in reply


def test_task_completion_time_reply_keeps_only_customer_safe_timing(adapter_module):
    reply = adapter_module._clean_support_reply(
        (
            "当前状态：已完成\n"
            "结论：该任务在海外执行，模型 dreamina，已生成回调。\n"
            "任务于 2026/05/13 21:28:58.470 开始执行，"
            "并在 2026/05/13 21:33:18 成功完成，耗时约 4 分 20 秒。"
            "内部定位来自 SLS logstore 和 /root/.hermes/session。"
        ),
        original_message="task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 这个任务的时间是什么，耗时多久？",
        language="zh-CN",
    )

    assert reply == "该任务已完成。开始时间：2026-05-13 21:28:58；完成时间：2026-05-13 21:33:18；耗时约 4 分 20 秒。"
    assert "海外" not in reply
    assert "dreamina" not in reply
    assert "模型" not in reply
    assert "回调" not in reply
    assert "MCP" not in reply
    assert "SLS" not in reply
    assert "logstore" not in reply
    assert "/root" not in reply


def test_task_completion_time_reply_uses_same_date_for_finish_time(adapter_module):
    reply = adapter_module._clean_support_reply(
        "当前状态：已完成。开始时间：2026/05/13 21:28:58.470，完成时间：21:33:18，耗时约 4分20秒。",
        original_message="task_3GTIlQ0WzIx8HPLvIRYIkUBY2fdgQuag 什么时候开始完成，耗时多久",
        language="zh-CN",
    )

    assert reply == "该任务已完成。开始时间：2026-05-13 21:28:58；完成时间：2026-05-13 21:33:18；耗时约 4 分 20 秒。"


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

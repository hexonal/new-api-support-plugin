from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import uuid
from typing import Any, Dict, Iterable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - exercised by check_requirements
    web = None


logger = logging.getLogger("gateway.platforms.new_api_support")

PLATFORM_NAME = "new_api_support"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9120
DEFAULT_PATH = "/new-api-support/chat"
DEFAULT_ALLOWED_SOURCES = ("new-api-web",)
DEFAULT_SESSION_PREFIX = "web_"
DEFAULT_MAX_BODY_BYTES = 65536
DEFAULT_MAX_MESSAGE_CHARS = 4000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _csv(value: Any, default: Iterable[str] = ()) -> list[str]:
    if value is None:
        return [item for item in default if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _env_first(name: str, extra: Dict[str, Any], key: str, default: Any = None) -> Any:
    return os.getenv(name) if os.getenv(name) is not None else extra.get(key, default)


def _sanitize_id(value: Any, default: str = "anonymous") -> str:
    text = str(value if value is not None else default).strip() or default
    return re.sub(r"[^A-Za-z0-9_.:@-]+", "_", text)[:160]


def check_new_api_support_requirements() -> bool:
    return web is not None


def _env_enablement() -> Optional[dict]:
    enabled = _truthy(os.getenv("NEW_API_SUPPORT_ENABLED"), False)
    token = os.getenv("NEW_API_SUPPORT_TOKEN")
    if not enabled and not token:
        return None
    return {
        "host": os.getenv("NEW_API_SUPPORT_HOST", DEFAULT_HOST),
        "port": _int_value(os.getenv("NEW_API_SUPPORT_PORT"), DEFAULT_PORT),
        "path": os.getenv("NEW_API_SUPPORT_PATH", DEFAULT_PATH),
        "token": token or "",
        "require_token": _truthy(os.getenv("NEW_API_SUPPORT_REQUIRE_TOKEN"), True),
        "allowed_sources": _csv(
            os.getenv("NEW_API_SUPPORT_ALLOWED_SOURCES"),
            DEFAULT_ALLOWED_SOURCES,
        ),
        "auto_skill": os.getenv("NEW_API_SUPPORT_AUTO_SKILL", ""),
        "session_prefix": os.getenv("NEW_API_SUPPORT_SESSION_PREFIX", DEFAULT_SESSION_PREFIX),
        "max_body_bytes": _int_value(
            os.getenv("NEW_API_SUPPORT_MAX_BODY_BYTES"),
            DEFAULT_MAX_BODY_BYTES,
        ),
        "max_message_chars": _int_value(
            os.getenv("NEW_API_SUPPORT_MAX_MESSAGE_CHARS"),
            DEFAULT_MAX_MESSAGE_CHARS,
        ),
        "request_timeout_seconds": _int_value(
            os.getenv("NEW_API_SUPPORT_REQUEST_TIMEOUT_SECONDS"),
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
    }


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    require_token = _truthy(
        _env_first("NEW_API_SUPPORT_REQUIRE_TOKEN", extra, "require_token", True),
        True,
    )
    token = _env_first("NEW_API_SUPPORT_TOKEN", extra, "token", "") or ""
    host = str(_env_first("NEW_API_SUPPORT_HOST", extra, "host", DEFAULT_HOST))
    if require_token and not token:
        logger.warning("New API Support: NEW_API_SUPPORT_TOKEN is required")
        return False
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        logger.warning("New API Support: refusing non-loopback bind without token")
        return False
    return True


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


class NewAPISupportAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig, **_: Any):
        Platform(PLATFORM_NAME)
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", {}) or {}

        self.host = str(_env_first("NEW_API_SUPPORT_HOST", extra, "host", DEFAULT_HOST))
        self.port = _int_value(_env_first("NEW_API_SUPPORT_PORT", extra, "port", DEFAULT_PORT), DEFAULT_PORT)
        self.path = str(_env_first("NEW_API_SUPPORT_PATH", extra, "path", DEFAULT_PATH) or DEFAULT_PATH)
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        self.token = str(_env_first("NEW_API_SUPPORT_TOKEN", extra, "token", "") or "")
        self.require_token = _truthy(
            _env_first("NEW_API_SUPPORT_REQUIRE_TOKEN", extra, "require_token", True),
            True,
        )
        self.allowed_sources = set(
            _csv(
                _env_first("NEW_API_SUPPORT_ALLOWED_SOURCES", extra, "allowed_sources", None),
                DEFAULT_ALLOWED_SOURCES,
            )
        )
        self.auto_skill = str(_env_first("NEW_API_SUPPORT_AUTO_SKILL", extra, "auto_skill", "") or "").strip() or None
        self.session_prefix = str(
            _env_first("NEW_API_SUPPORT_SESSION_PREFIX", extra, "session_prefix", DEFAULT_SESSION_PREFIX) or ""
        )
        self.max_body_bytes = _int_value(
            _env_first("NEW_API_SUPPORT_MAX_BODY_BYTES", extra, "max_body_bytes", DEFAULT_MAX_BODY_BYTES),
            DEFAULT_MAX_BODY_BYTES,
        )
        self.max_message_chars = _int_value(
            _env_first("NEW_API_SUPPORT_MAX_MESSAGE_CHARS", extra, "max_message_chars", DEFAULT_MAX_MESSAGE_CHARS),
            DEFAULT_MAX_MESSAGE_CHARS,
        )
        self.request_timeout_seconds = _int_value(
            _env_first(
                "NEW_API_SUPPORT_REQUEST_TIMEOUT_SECONDS",
                extra,
                "request_timeout_seconds",
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
            ),
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

        self._app: Any = None
        self._runner: Any = None
        self._site: Any = None
        self._pending_http_replies: Dict[str, asyncio.Future] = {}

    @property
    def name(self) -> str:
        return "New API Support"

    async def connect(self) -> bool:
        if web is None:
            self._set_fatal_error("missing_dependency", "aiohttp is not installed", retryable=False)
            return False
        if not validate_config(self.config):
            self._set_fatal_error("invalid_config", "New API support config is invalid", retryable=False)
            return False

        self._app = web.Application(client_max_size=self.max_body_bytes)
        self._app.router.add_get("/health", self.handle_health)
        self._app.router.add_post(self.path, self.handle_chat_request)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._mark_connected()
        logger.info("New API Support: listening on http://%s:%s%s", self.host, self.port, self.path)
        return True

    async def disconnect(self) -> None:
        for future in list(self._pending_http_replies.values()):
            if not future.done():
                future.cancel()
        self._pending_http_replies.clear()
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None
        self._mark_disconnected()

    async def handle_health(self, _request: Any) -> Any:
        return web.json_response(
            {
                "status": "ok",
                "platform": PLATFORM_NAME,
                "path": self.path,
            }
        )

    async def handle_chat_request(self, request: Any) -> Any:
        if request.method != "POST":
            return self._json({"error": "method_not_allowed"}, status=405)
        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error

        if request.content_length and request.content_length > self.max_body_bytes:
            return self._json({"error": "body_too_large"}, status=413)

        try:
            payload = await request.json()
        except Exception:
            return self._json({"error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return self._json({"error": "invalid_payload"}, status=400)

        validation_error = self._validate_payload(payload)
        if validation_error is not None:
            return validation_error
        if self._message_handler is None:
            return self._json({"error": "handler_not_ready"}, status=503)

        event = self._build_event(payload, request)
        try:
            reply = await asyncio.wait_for(
                self._call_handler(event),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._json({"error": "agent_timeout"}, status=504)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("New API Support: handler failed")
            return self._json({"error": "agent_error", "message": str(exc)}, status=500)

        return self._json(
            {
                "session_id": payload["session_id"],
                "reply": reply or "",
            }
        )

    async def _call_handler(self, event: MessageEvent) -> str:
        response = await self._message_handler(event)
        if response is not None:
            return str(response)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_http_replies[event.source.chat_id] = future
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)
        finally:
            self._pending_http_replies.pop(event.source.chat_id, None)

    def _validate_payload(self, payload: Dict[str, Any]) -> Optional[Any]:
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return self._json({"error": "missing_session_id"}, status=400)
        if len(session_id) > 160 or re.search(r"[\r\n\x00]", session_id):
            return self._json({"error": "invalid_session_id"}, status=400)
        if self.session_prefix and not session_id.startswith(self.session_prefix):
            return self._json({"error": "invalid_session_prefix"}, status=400)

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return self._json({"error": "missing_message"}, status=400)
        if len(message) > self.max_message_chars:
            return self._json({"error": "message_too_long"}, status=413)

        source = str(payload.get("source") or "new-api-web").strip()
        if self.allowed_sources and "*" not in self.allowed_sources and source not in self.allowed_sources:
            return self._json({"error": "source_not_allowed"}, status=403)
        return None

    def _build_event(self, payload: Dict[str, Any], request: Any) -> MessageEvent:
        source_name = str(payload.get("source") or "new-api-web").strip()
        session_id = str(payload["session_id"]).strip()
        user_id = _sanitize_id(payload.get("user_id", "anonymous"))
        user_name = str(payload.get("user_name") or payload.get("username") or user_id)
        hermes_user_id = f"{source_name}:{user_id}"
        message_id = str(payload.get("message_id") or uuid.uuid4())
        source = self.build_source(
            chat_id=session_id,
            chat_name="New API Web Support",
            chat_type="dm",
            user_id=hermes_user_id,
            user_name=user_name,
            thread_id=session_id,
            message_id=message_id,
        )
        return MessageEvent(
            text=str(payload["message"]).strip(),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=message_id,
            auto_skill=self.auto_skill,
        )

    def _check_auth(self, request: Any) -> Optional[Any]:
        if not self.require_token:
            return None
        if not self.token:
            return self._json({"error": "server_missing_token"}, status=503)
        header = getattr(request, "headers", {}).get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return self._json({"error": "unauthorized"}, status=401)
        supplied = header[len(prefix):].strip()
        if not hmac.compare_digest(supplied, self.token):
            return self._json({"error": "unauthorized"}, status=401)
        return None

    def _json(self, data: Dict[str, Any], status: int = 200) -> Any:
        return web.json_response(data, status=status)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        future = self._pending_http_replies.get(str(chat_id))
        if future is not None and not future.done():
            future.set_result(content)
        return SendResult(success=True, message_id=str(uuid.uuid4()), raw_response={"reply_to": reply_to, "metadata": metadata})

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return None

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None) -> SendResult:
        text = caption or image_url
        return await self.send(chat_id, text)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "New API Web Support", "type": "dm", "chat_id": str(chat_id)}


def register(ctx: Any) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="New API Support",
        adapter_factory=lambda cfg: NewAPISupportAdapter(cfg),
        check_fn=check_new_api_support_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NEW_API_SUPPORT_TOKEN"],
        install_hint="aiohttp is required and is included in the Hermes gateway environment.",
        env_enablement_fn=_env_enablement,
        allowed_users_env="NEW_API_SUPPORT_ALLOWED_USERS",
        allow_all_env="NEW_API_SUPPORT_ALLOW_ALL_USERS",
        max_message_length=DEFAULT_MAX_MESSAGE_CHARS,
        emoji="🌐",
        allow_update_command=False,
    )

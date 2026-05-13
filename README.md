# new-api Hermes Platform Plugin

`new-api` is a Hermes Agent platform plugin that exposes a local HTTP support endpoint for a New API web frontend.

The intended flow is:

1. Browser opens the New API support widget.
2. New API backend receives the browser message.
3. New API backend calls this plugin on the Hermes server with `Authorization: Bearer ...`.
4. Hermes handles the message as platform `new_api_support` and returns a support reply.

The browser should never call Hermes directly and should never receive the bearer token.

## Endpoint

Default listener:

```text
POST http://127.0.0.1:9120/new-api-support/chat
GET  http://127.0.0.1:9120/health
```

Request:

```json
{
  "session_id": "web_abc123",
  "message": "接口返回 403 怎么处理？",
  "source": "new-api-web",
  "user_id": 123,
  "role": 1,
  "context": {
    "page_url": "https://new-api.example.com/contact",
    "path": "/contact",
    "title": "联系我们",
    "client_ip": "203.0.113.10"
  }
}
```

Response:

```json
{
  "session_id": "web_abc123",
  "reply": "请把 request_id、curl 和报错时间发我，我来查。"
}
```

## Environment

Required:

```bash
NEW_API_SUPPORT_TOKEN=...
NEW_API_SUPPORT_ALLOW_ALL_USERS=true
```

Recommended defaults:

```bash
NEW_API_SUPPORT_ENABLED=true
NEW_API_SUPPORT_HOST=127.0.0.1
NEW_API_SUPPORT_PORT=9120
NEW_API_SUPPORT_PATH=/new-api-support/chat
NEW_API_SUPPORT_ALLOWED_SOURCES=new-api-web
NEW_API_SUPPORT_REQUIRE_TOKEN=true
NEW_API_SUPPORT_SESSION_PREFIX=web_
NEW_API_SUPPORT_MAX_BODY_BYTES=65536
NEW_API_SUPPORT_MAX_MESSAGE_CHARS=4000
NEW_API_SUPPORT_REQUEST_TIMEOUT_SECONDS=180
```

Use `NEW_API_SUPPORT_ALLOWED_USERS` instead of `NEW_API_SUPPORT_ALLOW_ALL_USERS=true` if the backend sends a fixed small set of user identities. In the web-support deployment, the bearer token is the primary boundary and Hermes user auth is usually set to allow authenticated widget traffic.

`NEW_API_SUPPORT_AUTO_SKILL` is optional. Keep it unset for a generic bridge, or set it in the deployment environment when this endpoint should always load a specific business skill.

## Hermes Config

Restrict the support platform toolset in `/root/.hermes/config.yaml`:

```yaml
platform_toolsets:
  new_api_support: [web, memory, no_mcp]
```

This keeps the customer support widget from inheriting the broader CLI tool surface.

## Install

Install it on the Hermes server with:

```bash
rm -rf /root/.hermes/plugins/new-api
git clone --depth 1 git@github.com:hexonal/new-api-support-plugin.git /root/.hermes/plugins/new-api
hermes plugins enable new-api
systemctl restart hermes-gateway
```

For updates:

```bash
cd /root/.hermes/plugins/new-api
git fetch origin main
git reset --hard origin/main
systemctl restart hermes-gateway
```

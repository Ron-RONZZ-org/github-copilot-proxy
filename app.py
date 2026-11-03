"""ronzz-copilot-proxy — GitHub Copilot "auto" as an OpenAI-compatible API.

Bridges Open WebUI (OpenAI-compatible connection with ``api_type="responses"``)
to the GitHub Copilot API:

  1. GitHub OAuth token  ->  ``Authorization: Bearer`` for the Copilot API host
  2. ``POST /models/session``       -> auto pool + ``Copilot-Session-Token`` (TTL ~1 h)
  3. ``POST /models/session/intent`` -> per-conversation model routing
  4. ``POST /responses``             -> the actual completion

On the ``/v1/responses`` path the proxy performs **no payload translation**:
Open WebUI converts Chat Completions to Responses itself (``api_type=responses``
-> ``convert_to_responses_payload``), so the proxy only resolves the model,
adds the Copilot headers and forwards the body.

Endpoint layout (all under the connection base URL, e.g.
``http://copilot-proxy:8080/v1``):

  GET  /v1/models            the ``auto`` model
  POST /v1/responses         Responses API (primary path used by Open WebUI)
  POST /v1/chat/completions  Chat Completions adapter (non-streaming)
  GET  /health               liveness + current auto session state
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import ClientResponse, ClientSession, ClientTimeout, web

LOG = logging.getLogger("copilot-proxy")

DEFAULT_API_BASE = "https://api.individual.githubcopilot.com"
DEFAULT_API_BASES = [DEFAULT_API_BASE, "https://api.githubcopilot.com"]
DEFAULT_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
DEFAULT_INTEGRATION_ID = "copilot-developer-cli"
DEFAULT_API_VERSION = "2025-07-16"
DEFAULT_EDITOR_VERSION = "vscode/1.99.3"
DEFAULT_EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
DEFAULT_USER_AGENT = "GitHubCopilotChat/0.26.7"
MODEL_ID = "auto"

CONTROL_TIMEOUT = ClientTimeout(total=30)
COMPLETION_TIMEOUT = ClientTimeout(total=600)
STREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=30, sock_read=600)

# Refresh the auto session this long before it actually expires.
SESSION_REFRESH_MARGIN = 120
# Routing decisions are sticky per conversation, like VS Code.
ROUTING_CACHE_MAX = 512


class ConfigError(RuntimeError):
    """Raised when the proxy configuration is missing or malformed."""


class CredentialRejected(RuntimeError):
    """Raised when GitHub rejects the configured credential."""


class CopilotSession:
    """An open GitHub Copilot auto session."""

    def __init__(
        self,
        token: str,
        expires_at: float,
        available_models: list[str],
        selected_model: str,
        api_base: str,
        bearer: str,
    ):
        self.token = token
        self.expires_at = expires_at
        self.available_models = available_models
        self.selected_model = selected_model
        self.api_base = api_base
        self.bearer = bearer

    @property
    def usable(self) -> bool:
        return bool(self.token) and self.expires_at - time.time() > SESSION_REFRESH_MARGIN

    def public(self) -> dict[str, Any]:
        return {
            "selected_model": self.selected_model,
            "available_models": self.available_models,
            "expires_at": int(self.expires_at),
            "expires_in": int(max(0, self.expires_at - time.time())),
            "api_base": self.api_base,
        }


class CopilotClient:
    """Talks to the GitHub Copilot API on behalf of one GitHub account.

    Two credential strategies are supported, tried in order:

    ``direct``
        The configured credential is sent as the bearer token straight to the
        Copilot API host. This is what the official Copilot CLI does with a
        GitHub OAuth token (``Authorization: Bearer <github oauth token>``).

    ``exchange``
        The configured credential is exchanged for a short-lived Copilot token
        via ``GET api.github.com/copilot_internal/v2/token`` (~30 min). This is
        the documented path for a fine-grained personal access token carrying
        the ``copilot_requests`` permission.

    The first strategy that opens a session wins and is reused until it stops
    working, so either credential type can be configured without code changes.
    """

    def __init__(self, config: dict[str, Any], session: ClientSession):
        self.config = config
        self.http = session
        configured = config.get("api_bases")
        if not configured:
            # A single legacy `api_base` wins; otherwise try every known host.
            configured = [config["api_base"]] if config.get("api_base") else DEFAULT_API_BASES
        self.api_bases = [str(base).rstrip("/") for base in configured]
        self.exchange_url = config.get("exchange_url", DEFAULT_EXCHANGE_URL)
        self.allow_exchange = bool(config.get("allow_exchange", True))
        self.github_token = config["github_token"]
        self.integration_id = config.get("integration_id", DEFAULT_INTEGRATION_ID)
        self.api_version = config.get("api_version", DEFAULT_API_VERSION)
        self.editor_version = config.get("editor_version", DEFAULT_EDITOR_VERSION)
        self.editor_plugin_version = config.get("editor_plugin_version", DEFAULT_EDITOR_PLUGIN_VERSION)
        self.user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
        self.model_hints = config.get("model_hints", ["auto"])
        self.intent_model_hints = config.get("intent_model_hints", ["auto"])

        self._session: CopilotSession | None = None
        self._session_lock = asyncio.Lock()
        self._routing: dict[str, tuple[str, float]] = {}
        self._exchanged: tuple[str, float] | None = None
        self._strategy: str | None = None

    # -- headers ---------------------------------------------------------

    def base_headers(self, bearer: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer or self.github_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Copilot-Integration-Id": self.integration_id,
            "Editor-Version": self.editor_version,
            "Editor-Plugin-Version": self.editor_plugin_version,
            "X-GitHub-Api-Version": self.api_version,
        }

    # -- credential strategies -------------------------------------------

    async def _exchange_token(self) -> str | None:
        """Exchange the credential for a short-lived Copilot token (cached)."""
        if not self.allow_exchange:
            return None
        if self._exchanged and self._exchanged[1] - time.time() > 60:
            return self._exchanged[0]

        headers = self.base_headers()
        headers.pop("X-GitHub-Api-Version", None)
        try:
            async with self.http.get(
                self.exchange_url,
                headers=headers,
                timeout=CONTROL_TIMEOUT,
            ) as resp:
                payload = await _read_json(resp)
                if resp.status != 200:
                    LOG.info("credential exchange rejected (HTTP %s)", resp.status)
                    return None
        except TimeoutError:
            LOG.warning("credential exchange timed out")
            return None
        except Exception as exc:  # noqa: BLE001 - any failure just disables the strategy
            LOG.info("credential exchange failed: %s", exc)
            return None

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            return None
        expires_at = float(payload.get("expires_at") or (time.time() + 1500))
        self._exchanged = (token, expires_at)
        LOG.info("exchanged credential for a Copilot token (valid %ss)", int(expires_at - time.time()))
        return token

    async def _strategies(self):
        """Yield (strategy, api_base, bearer) candidates, cheapest first.

        The credential exchange is only attempted once the direct strategy has
        failed, so an OAuth credential never pays for an unnecessary round trip.
        """
        for base in self.api_bases:
            yield ("direct", base, self.github_token)

        exchanged = await self._exchange_token()
        if exchanged:
            for base in self.api_bases:
                yield ("exchange", base, exchanged)

    # -- auto session ----------------------------------------------------

    async def ensure_session(self) -> CopilotSession:
        """Return a usable auto session, opening one if needed."""
        async with self._session_lock:
            if self._session and self._session.usable:
                return self._session

            last_error: Exception | None = None
            async for strategy, base, bearer in self._strategies():
                try:
                    session = await self._open_session(strategy, base, bearer)
                except CredentialRejected as exc:
                    last_error = exc
                    LOG.info("strategy %s on %s rejected the credential", strategy, base)
                    continue
                except RuntimeError as exc:
                    last_error = exc
                    LOG.info("strategy %s on %s failed: %s", strategy, base, exc)
                    continue

                self._session = session
                self._strategy = strategy
                LOG.info(
                    "opened Copilot auto session via %s on %s: selected=%s pool=%s expires_in=%ss",
                    strategy,
                    base,
                    session.selected_model,
                    ",".join(session.available_models),
                    int(session.expires_at - time.time()),
                )
                return session

            raise last_error or RuntimeError("Could not open a Copilot auto session with any strategy")

    async def _open_session(self, strategy: str, base: str, bearer: str) -> CopilotSession:
        body = {"auto_mode": {"model_hints": self.model_hints}}
        try:
            async with self.http.post(
                f"{base}/models/session",
                json=body,
                headers=self.base_headers(bearer),
                timeout=CONTROL_TIMEOUT,
            ) as resp:
                payload = await _read_json(resp)
                if resp.status != 200:
                    raise self._control_error(resp.status, payload, "models/session")
        except TimeoutError as exc:
            raise RuntimeError("Timed out opening a Copilot auto session") from exc

        token = payload.get("session_token")
        if not token:
            raise RuntimeError(f"Copilot auto session response had no session_token: {payload}")

        return CopilotSession(
            token=token,
            expires_at=float(payload.get("expires_at") or (time.time() + 1800)),
            available_models=list(payload.get("available_models") or []),
            selected_model=payload.get("selected_model") or "",
            api_base=base,
            bearer=bearer,
        )

    def _control_error(self, status: int, payload: Any, endpoint: str) -> Exception:
        detail = payload if isinstance(payload, str) else json.dumps(payload)[:400]
        if status in (401, 403):
            return CredentialRejected(
                f"GitHub rejected the credential on {endpoint} (HTTP {status}): {detail}. "
                "Replace the 'github_token' in the proxy config with a valid credential."
            )
        return RuntimeError(f"Copilot {endpoint} failed (HTTP {status}): {detail}")

    # -- routing ---------------------------------------------------------

    @staticmethod
    def _text_from_items(items: Any) -> list[str]:
        texts: list[str] = []
        if not isinstance(items, list):
            return texts
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        return texts

    def _conversation_key(self, payload: dict[str, Any]) -> str:
        """Stable per-conversation key: the first user message of the thread."""
        user_texts: list[str] = []
        for item in payload.get("input") or []:
            if isinstance(item, dict) and item.get("role") == "user":
                user_texts.extend(self._text_from_items([item]))
        seed = user_texts[0] if user_texts else json.dumps(payload.get("instructions") or "", sort_keys=True)
        return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()

    def _current_prompt(self, payload: dict[str, Any]) -> str:
        """The text of the latest user turn — what the intent router classifies."""
        last = ""
        for item in payload.get("input") or []:
            if isinstance(item, dict) and item.get("role") == "user":
                texts = self._text_from_items([item])
                if texts:
                    last = "\n".join(texts)
        return last[:8000]

    async def choose_model(self, session: CopilotSession, payload: dict[str, Any]) -> str:
        """Resolve the ``auto`` model for this conversation (sticky per thread)."""
        key = self._conversation_key(payload)
        cached = self._routing.get(key)
        if cached and cached[1] > time.time():
            return cached[0]

        prompt = self._current_prompt(payload)
        if not prompt:
            # Nothing to classify (e.g. a tool-only continuation): stay with the
            # model the session itself picked.
            chosen = session.selected_model or (session.available_models[0] if session.available_models else "")
            if chosen:
                self._remember(key, chosen, session.expires_at)
            return chosen

        body: dict[str, Any] = {"prompt": prompt, "available_models": session.available_models}
        if self.intent_model_hints:
            body["model_hints"] = self.intent_model_hints

        headers = self.base_headers(session.bearer)
        headers["Copilot-Session-Token"] = session.token
        try:
            async with self.http.post(
                f"{session.api_base}/models/session/intent",
                json=body,
                headers=headers,
                timeout=CONTROL_TIMEOUT,
            ) as resp:
                result = await _read_json(resp)
                if resp.status != 200:
                    raise self._control_error(resp.status, result, "models/session/intent")
        except TimeoutError as exc:
            raise RuntimeError("Timed out asking the Copilot intent router") from exc

        chosen = result.get("chosen_model") or session.selected_model
        if not chosen:
            raise RuntimeError(f"Copilot intent router returned no model: {result}")

        LOG.info(
            "routed auto -> %s (label=%s confidence=%s)",
            chosen,
            result.get("predicted_label"),
            result.get("confidence"),
        )
        self._remember(key, chosen, session.expires_at)
        return chosen

    def _remember(self, key: str, model: str, expires_at: float) -> None:
        if len(self._routing) >= ROUTING_CACHE_MAX:
            oldest = min(self._routing.items(), key=lambda kv: kv[1][1])[0]
            self._routing.pop(oldest, None)
        self._routing[key] = (model, min(expires_at, time.time() + 3600))

    # -- completion ------------------------------------------------------

    async def prepare(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], ClientResponse]:
        """Resolve the model, forward the payload upstream and return the response."""
        session = await self.ensure_session()
        model = await self.choose_model(session, payload)

        upstream = dict(payload)
        upstream["model"] = model

        headers = self.base_headers(session.bearer)
        headers["Copilot-Session-Token"] = session.token
        headers["Accept"] = "text/event-stream" if payload.get("stream") else "application/json"

        response = await self.http.post(
            f"{session.api_base}/responses",
            json=upstream,
            headers=headers,
            timeout=STREAM_TIMEOUT if payload.get("stream") else COMPLETION_TIMEOUT,
        )
        return upstream, response


async def _read_json(response: ClientResponse) -> Any:
    """Read a response body as JSON, falling back to the raw text.

    GitHub answers a rejected credential with a plain-text body ("403 Forbidden.
    For more on scraping GitHub ..."), so parsing must never raise.
    """
    text = await response.text()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {path}: {exc}") from exc

    if not config.get("github_token"):
        raise ConfigError(f"Config file {path} has no 'github_token'")
    if not config.get("shared_secret"):
        raise ConfigError(f"Config file {path} has no 'shared_secret'")
    return config


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": {"message": message, "type": "proxy_error"}}, status=status)


async def handle_health(request: web.Request) -> web.Response:
    client: CopilotClient = request.app["client"]
    session = client._session
    body: dict[str, Any] = {"status": "ok", "model": MODEL_ID, "session": session.public() if session else None}
    return web.json_response(body)


async def handle_models(request: web.Request) -> web.Response:
    """Only the ``auto`` model is exposed — the point of this proxy."""
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL_ID,
                    "object": "model",
                    "name": "GitHub Copilot (auto)",
                    "owned_by": "github-copilot",
                    "created": int(time.time()),
                }
            ],
        }
    )


def _authorise(request: web.Request) -> web.Response | None:
    client: CopilotClient = request.app["client"]
    header = request.headers.get("Authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if presented != client.config["shared_secret"]:
        return _error(401, "Invalid shared secret")
    return None


async def _stream_body(response: ClientResponse) -> AsyncIterator[bytes]:
    """Pass the upstream SSE stream through untouched."""
    try:
        async for chunk in response.content.iter_any():
            yield chunk
    finally:
        response.close()


async def handle_responses(request: web.Request) -> web.Response:
    denied = _authorise(request)
    if denied:
        return denied

    client: CopilotClient = request.app["client"]
    try:
        payload = await request.json()
    except Exception:
        return _error(400, "Request body is not valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object")

    try:
        _, response = await client.prepare(payload)
    except CredentialRejected as exc:
        LOG.error("%s", exc)
        return _error(502, str(exc))
    except (TimeoutError, RuntimeError) as exc:
        LOG.error("Copilot request failed: %s", exc)
        return _error(502, f"Copilot request failed: {exc}")

    if response.status >= 400:
        body = await response.text()
        response.close()
        LOG.error("Copilot upstream error %s: %s", response.status, body[:500])
        return web.Response(status=response.status, text=body, content_type="application/json")

    if payload.get("stream"):
        # aiohttp >= 3.14: StreamResponse takes no `body=` argument — the
        # upstream bytes are written directly to the prepared response.
        stream = web.StreamResponse(
            status=response.status,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await stream.prepare(request)
        try:
            async for chunk in _stream_body(response):
                await stream.write(chunk)
        finally:
            response.close()
        await stream.write_eof()
        return stream

    try:
        result = await response.json(content_type=None)
    finally:
        response.close()
    return web.json_response(result, status=response.status)


async def handle_chat_completions(request: web.Request) -> web.Response:
    """Chat Completions adapter (non-streaming only).

    Open WebUI talks to this proxy over the Responses API, so this endpoint
    exists for other clients and for graceful degradation if the connection is
    ever configured without ``api_type="responses"``.
    """
    denied = _authorise(request)
    if denied:
        return denied

    client: CopilotClient = request.app["client"]
    try:
        payload = await request.json()
    except Exception:
        return _error(400, "Request body is not valid JSON")

    if payload.get("stream"):
        return _error(501, "Streaming is only supported on /v1/responses")

    messages = payload.get("messages") or []
    instructions = ""
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            instructions = content if isinstance(content, str) else json.dumps(content)
            continue
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
        text_type = "output_text" if role == "assistant" else "input_text"
        input_items.append({"type": "message", "role": role, "content": [{"type": text_type, "text": text}]})

    responses_payload: dict[str, Any] = {"model": MODEL_ID, "input": input_items}
    if instructions:
        responses_payload["instructions"] = instructions
    if payload.get("max_tokens"):
        responses_payload["max_output_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        responses_payload["temperature"] = payload["temperature"]

    try:
        _, response = await client.prepare(responses_payload)
    except CredentialRejected as exc:
        LOG.error("%s", exc)
        return _error(502, str(exc))
    except (TimeoutError, RuntimeError) as exc:
        LOG.error("Copilot request failed: %s", exc)
        return _error(502, f"Copilot request failed: {exc}")

    if response.status >= 400:
        body = await response.text()
        response.close()
        return web.Response(status=response.status, text=body, content_type="application/json")

    try:
        result = await response.json(content_type=None)
    finally:
        response.close()

    content = ""
    for item in result.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    content += part.get("text", "")

    usage = result.get("usage") or {}
    return web.json_response(
        {
            "id": result.get("id", ""),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.get("model", MODEL_ID),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        },
        status=response.status,
    )


async def on_startup(app: web.Application) -> None:
    config = load_config(os.environ.get("CONFIG_PATH", "/app/config.json"))
    timeout = ClientTimeout(total=None, sock_connect=30, sock_read=600)
    app["http"] = ClientSession(timeout=timeout, trust_env=True)
    app["client"] = CopilotClient(config, app["http"])
    LOG.info("copilot-proxy started (api_bases=%s, model=%s)", ",".join(app["client"].api_bases), MODEL_ID)


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def create_app() -> web.Application:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

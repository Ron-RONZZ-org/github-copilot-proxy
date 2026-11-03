# github-copilot-proxy

![CI](https://github.com/Ron-RONZZ-org/github-copilot-proxy/actions/workflows/ci.yml/badge.svg)

GitHub Copilot **auto** model selection, exposed as an OpenAI-compatible API.

Built for [ronzz.ORG](https://ronzz.org)'s Open WebUI instance (`llm.ronzz.org`),
where GitHub Copilot is a **backup** model source. The account only has a
Copilot *student* (educational) seat, so the proxy pins the model to Copilot's
`auto` routing instead of exposing a hand-picked model list.

## Why this exists

Copilot's `auto` is **not a model id**. It is a client-side concept backed by
two server endpoints on the Copilot API:

1. `POST /models/session` with `{"auto_mode": {"model_hints": ["auto"]}}`
   opens an *auto session*: it returns the candidate pool
   (`available_models`), a `session_token` (valid ~1 h) and the session's
   default `selected_model`.
2. `POST /models/session/intent` with the conversation prompt and
   `available_models` is the *intent router*: it classifies the prompt
   (`needs_reasoning` / `no_reasoning`) and returns a ranked `candidate_models`
   list plus a `chosen_model`.

On top of that, the Copilot API requires a `Copilot-Session-Token` header on
every completion and a set of client-identification headers. The session token
expires hourly, so no static API key can work.

This proxy does that bookkeeping and forwards the request. It performs **no
payload translation**: Open WebUI is configured with `api_type: "responses"`,
which makes *it* convert Chat Completions to the Responses API before calling
`/v1/responses` here.

## Requirements

- A GitHub credential with Copilot access. Two types work:
  - a **GitHub OAuth token** (used directly as the bearer token), or
  - a **fine-grained personal access token** with the
    `copilot_requests` account permission (exchanged for a short-lived Copilot
    token via `GET api.github.com/copilot_internal/v2/token`).
- The credential's account must have an active Copilot seat.

The proxy tries the direct strategy first and falls back to the exchange
strategy, so either credential type works without a code change.

## API

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Lists the single `auto` model. |
| `POST /v1/responses` | Responses API — **the path Open WebUI uses**. Streaming and non-streaming. |
| `POST /v1/chat/completions` | Chat Completions adapter (non-streaming only). |
| `GET /health` | Liveness plus the current auto-session state. |

Every endpoint except `/health` requires `Authorization: Bearer <shared_secret>`.

## Configuration

`/app/config.json` (mount it read-only):

```json
{
  "github_token": "github_pat_…",
  "shared_secret": "…",
  "api_bases": [
    "https://api.individual.githubcopilot.com",
    "https://api.githubcopilot.com"
  ],
  "exchange_url": "https://api.github.com/copilot_internal/v2/token",
  "allow_exchange": true,
  "integration_id": "copilot-developer-cli",
  "api_version": "2025-07-16",
  "editor_version": "vscode/1.99.3",
  "editor_plugin_version": "copilot-chat/0.26.7",
  "model_hints": ["auto"],
  "intent_model_hints": ["auto"]
}
```

| Key | Meaning |
|---|---|
| `github_token` | The GitHub credential (PAT or OAuth token). |
| `shared_secret` | Bearer token Open WebUI must present. |
| `api_bases` | Copilot API hosts to try, in order. |
| `exchange_url` | The legacy credential-exchange endpoint. |
| `allow_exchange` | Set `false` to disable the `copilot_internal/v2/token` fallback. |
| `model_hints` | Sent to `/models/session` — `["auto"]` keeps the pool server-selected. |
| `intent_model_hints` | Sent to `/models/session/intent`. |

## Routing behaviour

The intent router's decision is **sticky per conversation**, keyed on the first
user message of the thread, so a chat does not change model mid-thread — the
same rule VS Code applies. A new conversation is routed afresh.

The routing cache is bounded (512 entries) and entries expire with the session.

## Run it

```bash
docker build -t github-copilot-proxy:v1 .
docker run --rm -p 127.0.0.1:8080:8080 \
  -v "$PWD/config.json:/app/config.json:ro" \
  github-copilot-proxy:v1
```

```bash
curl -s localhost:8080/health
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/responses \
  -H "Authorization: Bearer $SHARED_SECRET" -H 'Content-Type: application/json' \
  -d '{"model":"auto","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"Say OK"}]}]}'
```

## Open WebUI wiring

Register an OpenAI-compatible connection:

- Base URL: `http://copilot-proxy:8080/v1`
- API key: the `shared_secret`
- **Connection type / `api_type`: `responses`** — this is what makes Open WebUI
  call `/v1/responses` instead of `/v1/chat/completions`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt ruff pytest
.venv/bin/ruff check app.py tests/
.venv/bin/pytest tests/ -q
```

The unit tests cover the pure logic — prompt extraction, the conversation key
that makes routing sticky, and the configuration contract. The network paths
are exercised live against the Copilot API, not in the test suite.

| Path | Purpose |
|---|---|
| `app.py` | The whole proxy. |
| `tests/test_app.py` | Unit tests for the pure helpers. |
| `config.example.json` | Annotated configuration template. |
| `Dockerfile` | `python:3.12-slim` + aiohttp; runs as uid 1000 / gid 101. |
| `.github/workflows/ci.yml` | Ruff, pytest, and an image build + boot smoke test. |

## Deployment

The proxy runs inside the existing Open WebUI compose project on
`ronzz-linux-server-2`. Operational notes live on ronzz.ORG's internal wiki
(`RonzzIT:Runbook/CopilotProxy`); this repository holds only the source.

## Licence

MIT — see [LICENSE](LICENSE).

## Notes and caveats

- This is an **unofficial** integration. It is not affiliated with or supported
  by GitHub. Copilot API changes may break it without notice.
- Keep the proxy internal. It must never be exposed publicly without
  authentication, and it should be used within GitHub's
  [Acceptable Use Policies](https://docs.github.com/site-policy/acceptable-use-policies/github-acceptable-use-policies)
  and the
  [Copilot terms](https://docs.github.com/site-policy/github-terms/github-terms-for-additional-products-and-features#github-copilot).
- The `api_bases`/`integration_id`/`editor_version` values mirror what the
  official GitHub Copilot CLI sends, because the Copilot API is keyed to
  first-party client identifiers.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

`amb2api` is a FastAPI proxy that exposes the **AssemblyAI LLM Gateway** behind both **OpenAI** (`/v1/chat/completions`) and **Anthropic Messages** (`/v1/messages`, `/v1/messages/count_tokens`) compatible APIs. It also serves a Web management panel at `/ui` plus admin/account/playground/key-management JSON endpoints. The single entry point is `web.py`; routes are mounted from `src/api/*`.

`AGENTS.md` (in the repo root) is a sibling file with similar intent — keep both in sync when changing high-level guidance. `AGENTS.plan.md` is unrelated Codex CLI global config and is **not** a project doc.

## Common Commands

```bash
# One-shot dev start (creates .venv, runs `uv sync` if uv available, exports defaults, then `python web.py`)
./start.sh

# Manual setup (Python 3.12+ required — see pyproject.toml)
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"   # include dev group for pytest + hypothesis
python web.py

# Tests (require dev group — hypothesis is in [dependency-groups].dev, NOT in main deps)
python -m pytest -q
python -m pytest tests/test_anthropic_transfer.py -q
python -m pytest tests/test_daily_usage_limits.py::test_openai_router_passthroughs_error_status_from_gateway -q

# Docker
docker-compose up -d
```

The default panel/API password is `pwd` (set via `start.sh`). API: `http://127.0.0.1:7861/v1` — Panel: `http://127.0.0.1:7861/ui`.

There is **no requirements.txt** — `pyproject.toml` is the source of truth. `uv.lock` is gitignored; CI uses `pip install .` plus a separate `pytest` install.

## Architecture (cross-file picture)

### Request flow

The two compat surfaces share one upstream pipeline. The Anthropic surface re-enters the OpenAI machinery rather than duplicating it:

1. **Inbound conversion** (Anthropic only): `src/api/openai_router.py::anthropic_messages` → `src/transform/claude_to_openai.py::convert_claude_request_to_openai` produces an OpenAI-shaped dict, wrapped in `ChatCompletionRequest`.
2. **Common pipeline** (`src/services/assembly_client.py::send_assembly_request`):
   - Pulls the next API key via `KeySelector` (`src/services/key_selector.py`) using the configured `AggregationMode` (round_robin / random / fill_first).
   - Sanitizes messages (`_sanitize_messages`) — converts OpenAI tool-call shapes to AssemblyAI's `function_call` / `function_call_output` types. Preserves `reasoning_content` and `thoughtSignature` for Gemini thinking models.
   - Sends to AssemblyAI Gateway via `src/core/httpx_client.py`.
   - Parses rate-limit headers into `RateLimiter` (`src/services/rate_limiter.py`); 429/400 trigger key rotation + retry per `RETRY_429_*` config.
3. **Outbound conversion**:
   - OpenAI surface: `assembly_response_to_openai` (in `src/transform/openai_transfer.py`) → returned directly.
   - Anthropic surface: same OpenAI shape, then `src/transform/openai_to_claude.py::openai_response_to_anthropic` (non-stream) or `convert_openai_sse_to_anthropic_events` (stream). The stream wrapper lives in `openai_router.py::_convert_openai_stream_to_anthropic`.

### Streaming modes

Three modes coexist, gated by config + model name:

- **Real streaming** *(default)*: `get_enable_real_streaming()` is true (default since the 2026-04 Gateway relaunch) and the model is not in fake-stream list. Native upstream streaming carries OpenAI-shape `delta.tool_calls` and prompt-caching usage (`prompt_tokens_details.cached_tokens`, `prompt_tokens_details.cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`), surfaced through both compat surfaces.
- **Fake streaming**: model name starts with `假流式/` (see `is_fake_streaming_model` in `config.py`). Implemented in `src/services/assembly_stream_handler.py::fake_stream_response_for_assembly` — emits keepalive heartbeats while a non-stream upstream call runs, then yields the final content as one chunk.
- **Anti-truncation**: model name starts with `流式抗截断/` — drives multi-attempt continuation logic.

The `<feature>/<base-model>` prefix scheme is the toggle — `get_base_model_from_feature_model` strips it before going upstream.

### Storage

`src/storage/storage_adapter.py` is a Protocol-based adapter that selects exactly one backend at startup, in priority order: **Redis → Postgres → MongoDB → File** (default). The selection is environment-driven (`REDIS_URI`, `POSTGRES_DSN`, `MONGODB_URI`); file mode writes to `./creds/`. Redis keys are namespaced by `REDIS_PREFIX` (default `AMB2API`).

The same adapter holds four logical namespaces — credentials, credential state, **config**, **perf traces** (separate hash for cleanup), and **usage stats**. When changing schema, touch all four backends if the field is read by any of them.

### Configuration resolution

`config.py::get_config_value(key, default, env_var)` enforces this precedence:

1. Env var (only if `CONFIG_OVERRIDE_ENV` is truthy — defaults to `true` via `start.sh`).
2. Storage adapter `get_config(key)` (panel-writable via `/config/save`).
3. Hard-coded default.

Almost every getter in `config.py` is `async` and reads through this path. **Don't bypass it** — the panel relies on storage-resolved values.

### Auth

Two independent password checks:

- OpenAI surface (`/v1/models`, `/v1/chat/completions`): `Authorization: Bearer <API_PASSWORD>`.
- Anthropic surface (`/v1/messages*`): `x-api-key: <API_PASSWORD>` first, falls back to Bearer (`authenticate_anthropic_request`).
- Admin/account/keys/playground routes: `PANEL_PASSWORD` (the admin auth helper also accepts `API_PASSWORD` as a fallback).

`PASSWORD` env var, if set, overrides both. Errors on the Anthropic surface must be wrapped through `openai_error_to_anthropic_error` to keep `error.type` consistent (`authentication_error`, `rate_limit_error`, `invalid_request_error`).

### Stats vs. perf traces

`src/stats/unified_stats.py` is the source of truth for per-key/per-model usage; `src/stats/performance_tracker.py` writes per-request traces to a separate storage hash (`set_perf` / `get_perf` on the adapter). Daily quotas reset at **UTC 07:00** (`_get_next_utc_7am`).

## Modification rules (load-bearing)

- **Anthropic compatibility contract is frozen** at `docs/anthropic_compat_contract.md`. v1 scope = the three endpoints above + Anthropic-shaped `/v1/models` only when `anthropic-version` header is present. OpenAI defaults must remain unchanged.
- Keep changes minimal — no opportunistic refactors. The `AGENTS.md` checklist applies: when you change an API contract, update or add tests in the same change.
- Never log full API keys, session tokens, or message bodies. `account_api.py::_truncate_log_value` and `_summarize_headers_for_log` show the expected redaction style.
- Never introduce blocking I/O on async paths.
- When renaming a config key, grep at minimum: `config.py`, `src/api/admin_routes.py`, the relevant test files, and `front/control_panel.html` (the panel reads/writes config keys directly).
- Route or model changes likely affect `front/control_panel.html` — verify the panel still loads.

## Test layout

Tests live flat under `tests/` (the `py/`, `sh/`, `md/` subdirs in `tests/README.md` are gitignored historical paths). Notable suites:

- `test_anthropic_transfer.py`, `test_anthropic_api.py` — Anthropic conversion + route contract.
- `test_assembly_client_sanitize.py` — message normalization (tool-call shapes, thought signatures).
- `test_rate_limit_*.py`, `test_key_management*.py` — key rotation and rate-limiter persistence.
- `test_usage_aggregation.py`, `test_daily_usage_limits.py` — stats and quota.
- `test_fake_streaming.py`, `test_real_streaming_bootstrap.py`, `test_streaming_config.py` — streaming modes.
- `test_thought_signature.py` — Gemini thinking-model `thoughtSignature` round-trip.
- `test_prompt_caching.py` — Gateway prompt-caching pass-through (`cache_control` on messages, `prompt_cache_retention`/`prompt_cache_key` top-level, `cache_creation`/`cache_read_input_tokens` usage round-trip).

Several tests use **Hypothesis** (property-based) — they will fail to import if you skip the dev install.

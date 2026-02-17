# Anthropic Compatibility Contract (Phase 1 Baseline)

This document freezes the implementation scope for Anthropic compatibility in `amb2api`.

## Scope (v1)

The first delivery only adds these Anthropic-compatible capabilities:

1. `POST /v1/messages`
2. `POST /v1/messages/count_tokens`
3. `GET /v1/models` with Anthropic-shaped response only when Anthropic headers are present.

## Non-goals (v1)

1. No support for legacy Anthropic pre-Messages APIs.
2. No control panel toggle for protocol switching.
3. No storage schema migration.

## Compatibility Rules

1. Existing OpenAI paths remain stable:
   - `GET /v1/models` (OpenAI response by default)
   - `POST /v1/chat/completions`
2. Existing auth behavior for OpenAI clients remains valid.
3. New Anthropic auth path supports `x-api-key` and keeps Bearer compatibility.

## Route Behavior Matrix

| Request shape | Endpoint | Response shape |
|---|---|---|
| OpenAI | `/v1/chat/completions` | OpenAI |
| OpenAI | `/v1/models` | OpenAI |
| Anthropic | `/v1/messages` | Anthropic |
| Anthropic | `/v1/messages/count_tokens` | Anthropic |
| Anthropic headers | `/v1/models` | Anthropic |

## Audit References

- Plan source: `plan/2026-02-17_18-04-23-anthropic-compat-plan.md`
- Existing OpenAI routes: `src/api/openai_router.py`
- Existing app router mount: `web.py`

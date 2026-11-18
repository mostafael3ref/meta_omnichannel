# Connector API Requirements

Base URL (allowlisted): `https://connector.example.com`  
Auth: `X-API-Key: <tenant_api_key>`

## Confirmed (implemented in Frappe)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/facebook/conversations?limit&offset` | List conversations |
| GET | `/api/facebook/conversations/{page_id}/{sender_id}/messages?limit&offset` | Message history |
| POST | `/api/facebook/messages/send` | Outbound text (`page_id`, `recipient_id`, `text`, `idempotency_key`) |
| GET | `/api/instagram/conversations?limit&offset` | List Instagram conversations (Step 1) |

### Instagram list envelope (confirmed)

Top-level keys: `tenant_id`, `provider`, `total`, `conversations`, `limit`, `offset`, `has_more`, `next_offset`.

### Instagram conversation **item** fields

Required for DocType sync (fail closed if absent on non-empty items):

- `account_id` (required)
- `sender_id` (required)

Optional display fields (mapped when present/valid; never block sync):

- `last_message` → `last_message_preview` (max 500)
- `last_message_at` → `last_message_at` (ISO string or Unix seconds/ms)
- `last_message_direction` (reserved; not mapped in Step 1)
- `message_count` (reserved; not mapped in Step 1)

An empty `conversations: []` list is a successful zero-import sync.

Pagination fields: `total`, `limit`, `offset`, `has_more`, `next_offset`  
History: each batch chronological; offset `0` = newest batch.

## Pending (not invented / not called)

### Instagram

- Message history (exact path + item schema)
- Send text / media (outbound disabled in Frappe Step 1)
- OAuth asset linking

### WhatsApp

- List conversations
- Message history
- Session send text / media
- Template send
- Delivery status webhooks
- 24-hour window metadata

### Onboarding / OAuth

Suggested (not confirmed):

- `POST /api/meta/oauth/start` → `{ authorize_url, state }`
- `GET/POST /api/meta/oauth/callback`
- `GET /api/meta/assets`
- `POST /api/meta/assets/link`

### Incremental sync (recommended)

- Cursor-based conversation delta feed to avoid unbounded polling

## Frappe guarantees

- No Meta access tokens stored in Frappe
- API keys never sent to browser JS
- One site ↔ one connector tenant ID in Omnichannel Settings

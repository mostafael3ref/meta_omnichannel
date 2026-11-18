# Architecture

## Stack

| Layer | Target |
|-------|--------|
| Framework | Frappe v15 |
| CRM | Frappe CRM v1.91.1 (`crm`) |
| App | `meta_omnichannel` 0.2.0 |
| Connector | Configured HTTPS base URL (example: `https://connector.example.com`) |

## Data flow

```
Meta Platforms
    → External multi-tenant Meta connector (OAuth, tokens, Graph, webhooks)
        → Frappe meta_omnichannel (inbox, CRM links, local messages)
            → Frappe CRM (CRM Lead, Contact, users)
```

## Packages

| Package | Role |
|---------|------|
| `meta_connectors` | Pinned HTTPS client, SSRF, factory |
| `facebook_messenger` | Confirmed FB adapter, sync, outbound |
| `instagram` / `whatsapp` | Provider stubs + pending contracts |
| `crm_integration` | Lead/Contact/assign/notes |
| `api` | Whitelisted connector + inbox APIs |
| `onboarding` | Meta connect UI/state (pending OAuth APIs) |
| `jobs` | Background / scheduled sync |
| `www` + `public/frontend` | Unified inbox |

## DocTypes

- Omnichannel Settings (Single)
- Omnichannel Channel
- Omnichannel Conversation
- Omnichannel Message
- Omnichannel Sync Log
- Omnichannel Note

## Frontend

- Route: `/omnichannel`
- Vue 3 source: `frontend/`
- Shipped runtime: `meta_omnichannel/public/frontend/` (no-build bundle; replace via `npm run build` on Bench)

## Phase 1 security

See `docs/PHASE1_CONNECTOR.md` and `docs/SECURITY.md`.

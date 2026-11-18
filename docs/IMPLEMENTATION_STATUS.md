# Implementation Status

Last updated: 2026-11-18 (Windows source checkout — Bench / live CRM not executed)

| Phase | Status | Notes |
|-------|--------|-------|
| 1 Foundation & connector security | **Complete** | Settings, ConnectorClient, SSRF IP pinning, tests |
| 2 Facebook Messenger backend | **Complete (code)** | Adapter, sync, outbound, DocTypes; needs Bench + live connector |
| 3 Unified Inbox frontend | **Complete (code)** | Vue 3 source + no-build public bundle; npm build not run (npm missing) |
| 4 Frappe CRM integration | **Complete (code)** | Lead/Contact link, assign, notes, panel APIs |
| 5 Instagram Direct | **List + sync (code)** | Conversation list + DocType sync implemented and offline-tested. **Not** verified on a live Frappe CRM install. History, send, and OAuth remain pending. Not full Instagram support. |
| 6 WhatsApp Business | **Internal layer only** | Provider + models + window design; APIs pending |
| 7 Meta OAuth onboarding | **UI/stub only** | CSRF state + pending contracts documented |
| 8 Background jobs & notifications | **Complete (code)** | Enqueue helpers, hourly sync hook, assignment notifications |
| 9 Security & automated testing | **Partial** | Offline unit tests pass; Bench/IDOR integration tests pending |
| 10 Packaging & release docs | **Complete (docs)** | README + docs suite; install commands not executed here |

## Readiness

**Not production-ready.** Suitable for **staging on Linux Bench** after migrate, asset copy, connector credentials, and live Facebook / Instagram list-sync smoke tests.

## Checkpoint for continuation

If resuming in a new session:

1. Run offline tests: `python -m unittest discover -s meta_omnichannel/tests -v`
2. On Linux: `bench get-app` / install / migrate / optional `cd frontend && npm ci && npm run build`
3. Verify Instagram list + sync on a live site; keep history/send/OAuth disabled until contracts are confirmed in `docs/CONNECTOR_API_REQUIREMENTS.md`
4. WhatsApp remains pending

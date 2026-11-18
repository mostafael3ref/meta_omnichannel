# Meta Omnichannel CRM

Commercial Frappe application connecting **Facebook Messenger**, **Instagram** (list + sync), and WhatsApp (**pending**) to **Frappe CRM** via an external Meta connector.

| Item | Value |
|------|--------|
| App | `meta_omnichannel` |
| Title | Meta Omnichannel CRM |
| Version | 0.2.0 |
| Framework | Frappe **v15** |
| CRM | Frappe CRM **v1.91.1** (`crm`) |
| Connector | Configured HTTPS base URL (example: `https://connector.example.com`) |
| Status | Staging candidate — **not production-certified** |

## Architecture

Meta Platforms → External multi-tenant connector → this app → Frappe CRM

Frappe never stores Meta access tokens. The connector owns Graph/OAuth; this app owns inbox, CRM links, and sync.

## Channel status

| Channel | Status |
|---------|--------|
| Facebook Messenger | List, history, and send implemented (code); needs live Bench verification |
| Instagram | **Conversation list + sync** implemented and **offline-tested**. Not yet verified on a live Frappe CRM install. **History, send, and OAuth onboarding remain pending.** Not full Instagram support. |
| WhatsApp | Pending (internal stubs only) |

## Quick install (Linux Bench only)

```bash
bench get-app /path/to/meta_omnichannel
bench --site <site> install-app meta_omnichannel
bench --site <site> migrate
bench build --app meta_omnichannel
bench restart
```

Commands above were **not** run on the Windows development machine. See `docs/INSTALLATION.md`.

## Features by phase

1. Secure connector settings + SSRF-safe client  
2. Facebook list/history/send + local DocTypes  
3. Unified inbox UI (`/omnichannel`) with RTL/LTR  
4. CRM Lead/Contact linking, assign, notes  
5. Instagram conversation list + sync (offline-tested; history/send/OAuth pending)  
6. WhatsApp provider (pending connector APIs)  
7. Meta Connect UI stub + CSRF state (OAuth pending)  
8. Background sync jobs + notifications  
9. Offline security tests  
10. Packaging docs  

## Docs

- `docs/ARCHITECTURE.md`
- `docs/INSTALLATION.md`
- `docs/CONNECTOR_API_REQUIREMENTS.md`
- `docs/SECURITY.md`
- `docs/TESTING.md`
- `docs/DEPLOYMENT.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/PHASE1_CONNECTOR.md`

## Offline tests

```bash
python -m unittest discover -s meta_omnichannel/tests -v
```

## License

MIT — see `license.txt`.

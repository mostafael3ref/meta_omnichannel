# Testing

## Offline unit tests (Windows-safe)

```bash
python -m unittest discover -s meta_omnichannel/tests -v
```

Covers: SSRF, pinned HTTP, connector client, Facebook adapter, Instagram list/sync (adapter + gating + optional fields), unified `sync_inbox`, providers, idempotency, API stubs.

Does **not** contact a live connector (example host: `https://connector.example.com`).

Instagram conversation **list + sync** paths are offline-tested. They are **not** yet verified on a live Frappe CRM installation. Instagram history, send, and OAuth remain unimplemented / pending.

## Bench integration tests (Linux)

```bash
bench --site <site> run-tests --app meta_omnichannel
```

Includes DocType meta checks when Frappe is available.

## Manual staging checklist

1. Test Connection → Connected
2. Sync inbox → Facebook and Instagram conversations appear (list sync)
3. Open a Facebook thread → messages import
4. Send Facebook text → delivery_status Sent (or Failed with safe error)
5. Create Lead / assign / notes
6. Agent role cannot open others’ assigned conversations
7. Instagram: list + sync only — no history import, no composer/send, no OAuth success path
8. WhatsApp remains pending (no fake success)

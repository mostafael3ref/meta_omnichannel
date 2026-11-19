# Facebook Inbound Notifications (Connector → Frappe)

Phase 1 contract for near-real-time Facebook Messenger inbound message
notifications. **Scope: Frappe receiver only.** The Meta Connector must
implement the matching HTTP client; this document is the integration contract.

## Endpoint

```
POST https://<customer-frappe-site>/api/method/meta_omnichannel.facebook_messenger.api.notify_facebook_inbound
```

- Guest-accessible (`allow_guest=True`); **no Frappe user session**.
- Authenticated solely by per-site HMAC (see below).
- Do **not** send Frappe session cookies (CSRF is skipped only when the Guest
  session has no CSRF token — cookies from a logged-in browser can break the call).
- `Content-Type: application/json`
- Body must be the **exact** JSON bytes covered by the signature.
- Rate limit (Frappe `@rate_limit`): **180 requests / 60 seconds / client IP**
  so legitimate webhook bursts are supported while anonymous flooding is bounded.

## Secret provisioning

1. In Desk → **Omnichannel Settings** → **Rotate Inbound Notification Secret**
   (System Manager / Omnichannel Administrator), **or** call:

   `meta_omnichannel.facebook_messenger.api.rotate_inbound_notification_secret`

2. Store the returned plaintext on the Connector for this `connector_tenant_id`.
3. Field: `inbound_notification_secret` (Password). Encrypted at rest via
   Frappe password storage. **Never** returned to browser boot / Inbox JS.
4. **Do not** reuse `connector_api_key` (that key is Frappe → Connector only).

Until the secret is set, the endpoint returns HTTP 503 `secret_not_configured`.

### Secret rotation (hard cutover — required ops order)

Rotation replaces the only accepted HMAC secret immediately. There is **no**
dual-secret grace window in Phase 1.

**Safe order:**

1. Generate the new secret in Frappe (Desk button or `rotate_inbound_notification_secret`).
2. **Immediately** configure the same new secret on the Meta Connector for this tenant.
3. Confirm a test notification verifies (`ok: true`) before considering rotation done.
4. Only then discard the previous Connector-side secret.

If the Connector keeps the old secret after Frappe rotation, all notifications
fail with `401 signature_mismatch` until the Connector is updated. Prefer a
maintenance window or scripted rotate+push so steps 1–2 happen without delay.

## HMAC signing format

Headers:

| Header | Value |
|--------|--------|
| `X-Omnichannel-Timestamp` | Unix seconds (UTC), integer string |
| `X-Omnichannel-Signature` | `v1=<hex>` HMAC-SHA256 digest |
| `Content-Type` | `application/json` |

String to sign (bytes):

```text
UTF-8(timestamp) + ASCII('.') + raw_request_body
```

```text
signature = HMAC_SHA256(secret, string_to_sign).hexdigest()
header    = "v1=" + signature
```

Validation on Frappe:

- Freshness: `|now - timestamp| ≤ 300` seconds.
- Constant-time compare (`hmac.compare_digest`) of hex digests.
- Replay: Redis `SET key <ownership_token> NX EX 600` on `notification_id`
  (site-scoped via `frappe.cache().make_key`).
  - Duplicate → `{"ok": true, "duplicate": true}` (no enqueue).
  - Resolve/enqueue failure → ownership-safe Lua release so the Connector can retry.
  - Redis unavailable → HTTP 503 `replay_store_unavailable` (fail closed; no enqueue).

## Notification payload

```json
{
  "notification_id": "01JABCDEFEXAMPLE0000000000",
  "tenant_id": "tenant-matching-omnichannel-settings",
  "provider": "facebook",
  "event_type": "message.inbound",
  "page_id": "PAGE_ID",
  "sender_id": "PSID",
  "participant_name": "Ignored by Frappe (tip-only)",
  "preview": "Ignored by Frappe (tip-only)",
  "message_timestamp": 1732000000,
  "is_echo": false,
  "direction": "inbound"
}
```

`participant_name`, `preview`, and timestamps in the tip are **ignored** for
conversation writes. Message content and preview freshness come only from
`sync_conversation_messages` after enqueue.

### Required fields (accepted events)

| Field | Rules |
|-------|--------|
| `notification_id` | Stable unique id from Connector (replay key) |
| `tenant_id` | Must equal Omnichannel Settings `connector_tenant_id` |
| `provider` | `facebook` \| `facebook_messenger` \| `messenger` |
| `event_type` | `message.inbound` \| `facebook.message.inbound` |
| `page_id` | Must match Facebook `Omnichannel Channel` with `is_enabled=1` **and** `status=Connected` |
| `sender_id` | Messenger PSID / participant id |

Channel `status` options: `Draft`, `Connected`, `Disconnected`, `Error`,
`Pending Connector Support`. Only **Connected** + enabled pages are accepted.

### Ignored safely (`ok: true`, `ignored: true`)

- Echoes (`is_echo` / `echo` truthy)
- Outbound direction
- Unsupported `provider` / `event_type` (delivery receipts, reads, Instagram, etc.)

### Rejected

| Code | HTTP | Cause |
|------|------|--------|
| `signature_mismatch` / `signature_malformed` / `timestamp_stale_or_invalid` | 401 | Auth |
| `secret_not_configured` / `tenant_not_configured` / `replay_store_unavailable` / `enqueue_failed` | 503 | Setup / retryable |
| `tenant_mismatch` / `page_not_linked` | 403 | Wrong tenant / page not Connected+enabled |
| `notification_id_required` / `page_id_required` / `sender_id_required` | 400 | Schema |
| `untrusted_routing_fields` | 400 | Payload includes `conversation`, `conversation_name`, or `job` |

Frappe **never trusts** caller-supplied conversation names, site URLs, or job
paths. Conversation identity is always `page_id:sender_id` (local resolve only).

## Processing model

1. Verify HMAC + timestamp over the **exact** raw body (no alternate payload).
2. Classify event (accept / ignore).
3. Validate tenant + Connected/enabled page + sender.
4. Claim `notification_id` with ownership token (fail closed if Redis down).
5. Resolve/create conversation tip-only (no metadata overwrite on existing rows).
6. `enqueue_conversation_message_sync(conversation)` →
   `run_conversation_message_sync` → `sync_conversation_messages`
   (deduplicated job name `omnichannel_msg_sync:<name>`).
7. On resolve/enqueue failure: release **this** reservation token only; return 503.
8. On success: keep reservation; return
   `{"ok": true, "queued": true, "conversation": "...", ...}`.

Message bodies are pulled asynchronously from the Connector API by the worker.
Manual Sync and hourly_long bounded sync remain the fallback.

## Realtime (Inbox refresh hint)

After a **successful** message sync job, Frappe publishes:

```text
event: omnichannel_conversation_updated
data:  { "conversation": "<name>", "provider": "Facebook", "source": "message_sync", "processed": <int> }
```

Site-scoped; no message text, tokens, or PII beyond the conversation docname.
Inbox clients may subscribe later; Phase 1 does not change the production
plain-JS bundle.

## Connector integration checklist

1. Provision `inbound_notification_secret` per Frappe site/tenant.
2. On verified Meta webhook for an inbound Messenger message (non-echo):
   - Persist in Connector SQLite as today.
   - Resolve tenant from page subscription.
   - POST signed notification to that tenant’s Frappe site URL.
3. Retry on HTTP 503 (`replay_store_unavailable`, `enqueue_failed`) with the
   **same** `notification_id`.
4. Treat `duplicate: true` as success (already accepted).
5. Do not embed Connector API keys or Meta tokens in the notification.
6. Keep hourly / manual sync — notifications are an acceleration path.

## Out of scope (Phase 1)

- Meta Connector code changes in this repository
- Instagram / WhatsApp push
- Frontend realtime subscription wiring
- Dual-secret rotation / secret-management framework
- Creating Omnichannel Channels from unknown `page_id` values

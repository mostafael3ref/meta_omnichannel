# Security

## Connector credentials

- Stored in Omnichannel Settings Password field
- Read only server-side via `get_decrypted_password`
- Never returned from whitelisted methods or logged

## Inbound notification HMAC (Connector → Frappe)

- Dedicated Password field `inbound_notification_secret` (not the Connector API key)
- Requests signed with HMAC-SHA256 over `timestamp + '.' + raw_body`
- Constant-time signature compare; 300s timestamp skew; Redis replay on `notification_id`
- Guest endpoint; Connector must not send Frappe session cookies
- Full contract: `docs/FACEBOOK_INBOUND_NOTIFICATIONS.md`

## SSRF / DNS rebinding

- Code allowlist: approved connector hostname(s) only (docs example: `connector.example.com`)
- Resolve DNS once; reject private/loopback/link-local/metadata/multicast/reserved
- Connect to validated IP; TLS SNI/Host/verify use original hostname
- No redirects; proxies disabled (`trust_env=False`)

Residual: compromised DNS to attacker **public** IP still requires valid TLS cert for the hostname; use egress firewall controls.

## Authorization

- Roles: System Manager, Omnichannel Administrator, Omnichannel Agent
- Conversation/message permission queries restrict agents to assigned/unassigned
- Inbox and CRM APIs call `ensure_omnichannel_agent` / admin checks

## CSRF / session

- Inbox uses Frappe session + CSRF token from boot
- OAuth state stored server-side (when connector OAuth is enabled)

## Messaging safety

- Outbound idempotency keys prevent duplicate sends
- Message dedup by external id / payload hash
- Do not fabricate delivery receipts

## Meta tokens

Never stored in Frappe. Connector owns Meta OAuth tokens.

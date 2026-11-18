# Phase 1 — Secure Connector Integration

## Overview

Phase 1 connects each Frappe site to one tenant on the external FastAPI connector
(example base URL: `https://connector.example.com`) using a site-local **Omnichannel Settings**
Single DocType.

## Configuration

| Field | Type | Notes |
|-------|------|--------|
| Connector Base URL | Data | HTTPS only; hostname must be on the **code allowlist** |
| Connector Tenant ID | Data | One Frappe site ↔ one connector tenant |
| Connector API Key | Password | Encrypted; never shown in full after save |
| Connection Status | Select (read-only) | Updated by the connection test |
| Last Successful Test | Datetime (read-only) | |
| Last Error Summary | Small Text (read-only) | Safe summary only — no secrets |

## API

- Method: `meta_omnichannel.api.connector.test_connector_connection`
- HTTP (Desk): `/api/method/meta_omnichannel.api.connector.test_connector_connection`
- Probe: `GET {base}/api/facebook/conversations?limit=1&offset=0`
- Auth header: `X-API-Key: <key>`
- Redirects: not followed

## Security approach (SSRF / DNS rebinding)

**Chosen design: IP-pinned HTTPS + code-level host allowlist**

1. **Allowlist** (`ALLOWED_CONNECTOR_HOSTS` in `utils/constants.py`): only the
   approved connector hostname(s) compiled into the app are accepted (docs
   example: `connector.example.com`). Operators cannot expand this via
   Omnichannel Settings.
2. **DNS resolve once** → reject if any answer is loopback/private/link-local/
   metadata/multicast/reserved (IPv4 and IPv6).
3. **Connect to the validated IP** in the request URL (`https://<ip>/...`), not
   to the hostname. This removes the requests/urllib3 second DNS lookup that
   caused the validation→connection TOCTOU / DNS rebinding gap.
4. **TLS stays hostname-based**: custom `HostnameTLSAdapter` sets
   `server_hostname` and `assert_hostname` to the original hostname; HTTP
   `Host` matches; `verify=True` always; verification cannot be disabled.
5. **Proxies disabled**: `trust_env=False`, per-request `proxies` map cleared,
   and the HTTPS adapter refuses proxy managers.

### What this does *not* claim

- **Allowlisting alone does not solve DNS rebinding.** Rebinding protection
  comes from connecting to the validated IP.
- **Pinning does not prove connector authenticity by itself.** A compromised
  DNS answer for an allowlisted name could still point at an attacker
  **public** IP at resolve time. Authenticity relies on TLS certificates for
  that hostname (PKI) plus operational egress controls.
- Recommended network controls: firewall/DNS egress allowlist to the connector
  hostname/IPs; restrict Bench hosts from arbitrary outbound HTTPS where
  possible.

## Remaining risks

| Risk | Mitigation status |
|------|-------------------|
| DNS rebinding to private/metadata after validation | Mitigated by IP pinning |
| User-configured URL to arbitrary hosts | Mitigated by code allowlist |
| Env HTTP(S)_PROXY bypass | Mitigated (`trust_env=False`, proxies cleared) |
| Redirect to internal URL | Mitigated (`allow_redirects=False`, 3xx fail closed) |
| Compromised DNS → attacker public IP + stolen/forged cert | Residual — needs PKI + egress controls |
| Application bugs in urllib3 TLS hostname plumbing | Covered by unit tests; validate on Linux Bench |

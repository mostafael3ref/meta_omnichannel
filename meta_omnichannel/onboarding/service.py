"""Meta OAuth / asset onboarding — Frappe-side flow with pending connector contracts."""

from __future__ import annotations

import secrets

import frappe
from frappe import _

from meta_omnichannel.meta_connectors.permissions import ensure_omnichannel_admin

# Pending connector endpoints (do not call until confirmed):
PENDING_OAUTH_START = "/api/meta/oauth/start"  # expected
PENDING_OAUTH_CALLBACK = "/api/meta/oauth/callback"  # expected
PENDING_ASSETS_LIST = "/api/meta/assets"  # expected
PENDING_ASSETS_LINK = "/api/meta/assets/link"  # expected


@frappe.whitelist()
def get_onboarding_status() -> dict:
	ensure_omnichannel_admin()
	settings = frappe.get_single("Omnichannel Settings")
	channels = frappe.get_all(
		"Omnichannel Channel",
		fields=["name", "provider", "channel_name", "status", "external_account_id", "is_enabled"],
		order_by="modified desc",
	)
	return {
		"tenant_id": settings.connector_tenant_id,
		"connection_status": settings.connection_status,
		"channels": channels,
		"oauth": {
			"available": False,
			"reason": "Connector OAuth/onboarding API contract is not finalized",
			"pending_endpoints": [
				PENDING_OAUTH_START,
				PENDING_OAUTH_CALLBACK,
				PENDING_ASSETS_LIST,
				PENDING_ASSETS_LINK,
			],
		},
	}


@frappe.whitelist()
def begin_meta_connect() -> dict:
	"""Start Meta connect flow server-side.

	Returns a pending state until the connector exposes OAuth start URL APIs.
	Does not expose API keys to the browser.
	"""
	ensure_omnichannel_admin()
	state = secrets.token_urlsafe(24)
	# Store CSRF/state in cache bound to user + tenant
	settings = frappe.get_single("Omnichannel Settings")
	cache_key = f"omnichannel_oauth_state:{frappe.session.user}:{settings.connector_tenant_id}"
	frappe.cache().set_value(cache_key, state, expires_in_sec=600)
	return {
		"ok": False,
		"pending_contract": True,
		"state": state,
		"message": _(
			"Connect Meta Account is not live yet. The connector must expose OAuth start/callback APIs."
		),
		"pending_endpoints": [PENDING_OAUTH_START, PENDING_OAUTH_CALLBACK],
	}


@frappe.whitelist()
def complete_meta_connect(state: str, payload: str | None = None) -> dict:
	"""Complete onboarding after connector callback (stub until contract exists)."""
	ensure_omnichannel_admin()
	settings = frappe.get_single("Omnichannel Settings")
	cache_key = f"omnichannel_oauth_state:{frappe.session.user}:{settings.connector_tenant_id}"
	expected = frappe.cache().get_value(cache_key)
	if not expected or expected != state:
		frappe.throw(_("Invalid or expired OAuth state"), frappe.ValidationError)
	frappe.cache().delete_value(cache_key)
	return {
		"ok": False,
		"pending_contract": True,
		"message": _("Connector asset-linking API is not available yet."),
		"pending_endpoints": [PENDING_ASSETS_LIST, PENDING_ASSETS_LINK],
	}

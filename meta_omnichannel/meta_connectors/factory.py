"""Build ConnectorClient instances from Omnichannel Settings (server-side only)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from meta_omnichannel.meta_connectors.client import ConnectorClient
from meta_omnichannel.meta_connectors.errors import ConnectorConfigError

SETTINGS_DOCTYPE = "Omnichannel Settings"


def get_connector_api_key() -> str:
	api_key = get_decrypted_password(
		SETTINGS_DOCTYPE,
		SETTINGS_DOCTYPE,
		"connector_api_key",
		raise_exception=False,
	)
	if not api_key:
		raise ConnectorConfigError(_("Connector API Key is not set"))
	return api_key


def get_connector_client() -> ConnectorClient:
	"""Return a configured ConnectorClient. Never call from frontend-exposed code paths that echo secrets."""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	base_url = (settings.connector_base_url or "").strip()
	tenant_id = (settings.connector_tenant_id or "").strip()
	if not base_url or not tenant_id:
		raise ConnectorConfigError(_("Connector Base URL and Tenant ID are required"))
	api_key = get_connector_api_key()
	try:
		return ConnectorClient(base_url=base_url, api_key=api_key, tenant_id=tenant_id)
	finally:
		del api_key

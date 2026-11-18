"""Whitelisted connector administration APIs."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

from meta_omnichannel.meta_connectors.client import ConnectorClient
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.meta_connectors.permissions import ensure_omnichannel_admin
from meta_omnichannel.utils.logger import get_logger

SETTINGS_DOCTYPE = "Omnichannel Settings"


def _get_api_key() -> str:
	"""Read the connector API key via Frappe Password storage. Never log the value."""
	api_key = get_decrypted_password(
		SETTINGS_DOCTYPE,
		SETTINGS_DOCTYPE,
		"connector_api_key",
		raise_exception=False,
	)
	if not api_key:
		frappe.throw(_("Connector API Key is not set"), frappe.ValidationError)
	return api_key


def _safe_error_summary(message: str, *, max_len: int = 440) -> str:
	"""Truncate operator-facing error text; never include secrets."""
	text = (message or "").replace("\n", " ").strip()
	# Defense in depth if a future caller concatenates headers by mistake.
	lowered = text.lower()
	if "x-api-key" in lowered or "api_key" in lowered or "apikey" in lowered:
		return "Connector error (details redacted)"
	if len(text) > max_len:
		return text[: max_len - 3] + "..."
	return text


def _update_settings_status(*, connection_status: str, error_summary: str | None, success: bool) -> None:
	# Use set_value to avoid re-entering full document save password handling.
	values = {
		"connection_status": connection_status,
		"last_error_summary": error_summary or "",
	}
	if success:
		values["last_successful_test"] = now_datetime()
		values["last_error_summary"] = ""
	frappe.db.set_value(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, values, update_modified=True)
	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)


@frappe.whitelist()
def test_connector_connection() -> dict:
	"""Test connectivity to the external connector.

	Uses ``GET /api/facebook/conversations?limit=1&offset=0`` with ``X-API-Key``.
	Restricted to System Manager / Omnichannel Administrator.
	Never returns the API key.
	"""
	ensure_omnichannel_admin()

	if not frappe.has_permission(SETTINGS_DOCTYPE, "write"):
		frappe.throw(_("Not permitted to update Omnichannel Settings"), frappe.PermissionError)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	base_url = (settings.connector_base_url or "").strip()
	tenant_id = (settings.connector_tenant_id or "").strip()

	if not base_url or not tenant_id:
		_update_settings_status(
			connection_status="Misconfigured",
			error_summary="Connector Base URL and Tenant ID are required",
			success=False,
		)
		frappe.throw(_("Connector Base URL and Tenant ID are required"), frappe.ValidationError)

	logger = get_logger()
	logger.info(
		"connector_test_started base_url=%s tenant_id=%s",
		base_url,
		tenant_id,
	)

	try:
		api_key = _get_api_key()
		client = ConnectorClient(base_url=base_url, api_key=api_key, tenant_id=tenant_id)
		# api_key must not linger in locals longer than needed; client holds it.
		del api_key
		result = client.test_connection()
	except ConnectorError as exc:
		summary = _safe_error_summary(exc.message)
		_update_settings_status(
			connection_status=exc.connection_status,
			error_summary=summary,
			success=False,
		)
		logger.info(
			"connector_test_failed status=%s summary=%s",
			exc.connection_status,
			summary,
		)
		return {
			"ok": False,
			"status_code": None,
			"connection_status": exc.connection_status,
			"message": summary,
		}
	except Exception:
		summary = "Unexpected error while testing the connector"
		_update_settings_status(
			connection_status="Error",
			error_summary=summary,
			success=False,
		)
		logger.exception("connector_test_unexpected_error")
		return {
			"ok": False,
			"status_code": None,
			"connection_status": "Error",
			"message": summary,
		}

	_update_settings_status(
		connection_status=result.connection_status,
		error_summary=None if result.ok else result.message,
		success=result.ok,
	)
	logger.info(
		"connector_test_finished ok=%s http_status=%s connection_status=%s",
		result.ok,
		result.status_code,
		result.connection_status,
	)

	return {
		"ok": result.ok,
		"status_code": result.status_code,
		"connection_status": result.connection_status,
		"message": result.message,
	}

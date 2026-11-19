"""Whitelisted Facebook Messenger entrypoints.

Guest inbound notifications are authenticated via per-site HMAC
(``inbound_notification_secret``), not Frappe session cookies.
"""

from __future__ import annotations

import secrets

import frappe
from frappe import _
from frappe.utils.password import set_encrypted_password

from meta_omnichannel.facebook_messenger.inbound import (
	INBOUND_SECRET_FIELD,
	InboundNotificationError,
	SETTINGS_DOCTYPE,
	process_facebook_inbound_notification,
)
from meta_omnichannel.meta_connectors.permissions import ensure_omnichannel_admin

try:
	from frappe.rate_limiter import rate_limit
except ImportError:  # Offline unit tests / non-Bench environments
	def rate_limit(**_kwargs):  # type: ignore[misc]
		def _decorator(fn):
			return fn

		return _decorator


# Connector may burst many verified Meta webhook tips; keep headroom per IP.
_INBOUND_RATE_LIMIT = 180
_INBOUND_RATE_WINDOW_SEC = 60


def _read_raw_body() -> bytes:
	request = getattr(frappe, "request", None)
	if request is None:
		return b""
	get_data = getattr(request, "get_data", None)
	if callable(get_data):
		data = get_data(as_text=False)
		if isinstance(data, bytes):
			return data
		if isinstance(data, str):
			return data.encode("utf-8")
	raw = getattr(request, "data", None)
	if isinstance(raw, bytes):
		return raw
	if isinstance(raw, str):
		return raw.encode("utf-8")
	return b""


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=_INBOUND_RATE_LIMIT, seconds=_INBOUND_RATE_WINDOW_SEC, methods=["POST"])
def notify_facebook_inbound() -> dict:
	"""Connector → Frappe inbound message notification (HMAC-authenticated).

	Validates signature over the exact request body, checks tenant/page mapping,
	resolves the conversation locally, and enqueues ``run_conversation_message_sync``.
	Never performs Connector HTTP calls inline.
	"""
	# Auth is HMAC-only. Connector must omit Frappe session cookies so Guest CSRF
	# validation does not require a token (see docs/FACEBOOK_INBOUND_NOTIFICATIONS.md).
	body = _read_raw_body()
	try:
		return process_facebook_inbound_notification(body=body)
	except InboundNotificationError as exc:
		frappe.local.response["http_status_code"] = exc.status
		return {"ok": False, "error": exc.message, "code": exc.code}


@frappe.whitelist()
def rotate_inbound_notification_secret() -> dict:
	"""Generate a new inbound HMAC secret (admin only). Returns plaintext once."""
	ensure_omnichannel_admin()
	secret = secrets.token_urlsafe(32)
	set_encrypted_password(
		SETTINGS_DOCTYPE,
		SETTINGS_DOCTYPE,
		secret,
		fieldname=INBOUND_SECRET_FIELD,
	)
	# Touch modified so Desk reflects a change without storing plaintext.
	frappe.db.set_value(
		SETTINGS_DOCTYPE,
		SETTINGS_DOCTYPE,
		"modified",
		frappe.utils.now_datetime(),
		update_modified=False,
	)
	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
	return {
		"ok": True,
		"secret": secret,
		"field": INBOUND_SECRET_FIELD,
		"message": _(
			"Store this secret on the Meta Connector for this tenant before "
			"discarding the previous value. Rotation is a hard cutover — see "
			"docs/FACEBOOK_INBOUND_NOTIFICATIONS.md."
		),
	}

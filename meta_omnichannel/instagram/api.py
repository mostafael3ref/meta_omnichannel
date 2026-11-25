"""Whitelisted Instagram Messaging entrypoints.

Guest inbound notifications reuse the same per-site HMAC secret as Facebook
(``inbound_notification_secret``), not Frappe session cookies.
"""

from __future__ import annotations

import frappe

from meta_omnichannel.facebook_messenger.inbound import InboundNotificationError
from meta_omnichannel.instagram.inbound import process_instagram_inbound_notification

try:
	from frappe.rate_limiter import rate_limit
except ImportError:  # Offline unit tests / non-Bench environments
	def rate_limit(**_kwargs):  # type: ignore[misc]
		def _decorator(fn):
			return fn

		return _decorator


# Match Facebook inbound headroom so Connector bursts stay within limit.
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
def notify_instagram_inbound() -> dict:
	"""Connector → Frappe Instagram inbound tip (HMAC-authenticated).

	Validates signature over the exact request body, checks tenant/account mapping,
	resolves the conversation locally, and enqueues
	``run_instagram_conversation_message_sync`` (publishes Real-Time only after a
	successful message sync). Never performs Connector HTTP calls inline.
	"""
	# Auth is HMAC-only. Connector must omit Frappe session cookies so Guest CSRF
	# validation does not require a token (same contract as Facebook inbound).
	body = _read_raw_body()
	try:
		return process_instagram_inbound_notification(body=body)
	except InboundNotificationError as exc:
		frappe.local.response["http_status_code"] = exc.status
		return {"ok": False, "error": exc.message, "code": exc.code}

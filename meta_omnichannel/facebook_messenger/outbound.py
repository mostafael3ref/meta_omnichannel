"""Outbound Facebook text sending with local persistence and idempotency."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from meta_omnichannel.facebook_messenger.adapter import FacebookConnectorAdapter
from meta_omnichannel.meta_connectors.errors import (
	ConnectorError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.utils.idempotency import build_outbound_idempotency_key

_CONFIRMED_OK = frozenset({"Sent", "Delivered", "Read"})
_UNCERTAIN = frozenset({"Unknown", "Queued", "Pending"})
# Ambiguous outcomes: Meta may already have accepted the send.
_UNCERTAIN_EXCEPTIONS = (ConnectorUnreachableError, ConnectorServerError)


def _is_duplicate_idempotency_error(exc: BaseException) -> bool:
	"""True when insert lost a race on the unique idempotency_key."""
	name = type(exc).__name__
	if name in ("UniqueValidationError", "DuplicateEntryError", "IntegrityError"):
		return True
	msg = str(exc).lower()
	return "duplicate" in msg and ("idempotency" in msg or "unique" in msg or "entry" in msg)


def _response_for_existing(existing, *, deduplicated: bool) -> dict:
	"""Map a persisted outbound row to the API payload (never calls Meta)."""
	status = existing.delivery_status or "Unknown"
	payload = {
		"message": existing.name,
		"delivery_status": status,
		"deduplicated": deduplicated,
	}
	if status in _CONFIRMED_OK:
		payload["ok"] = True
		payload["uncertain"] = False
		return payload
	if status in _UNCERTAIN:
		payload["ok"] = False
		payload["uncertain"] = True
		payload["error"] = _("Delivery has not been confirmed")
		return payload
	payload["ok"] = False
	payload["uncertain"] = False
	payload["error"] = (existing.error_summary or _("Send failed"))[:440]
	return payload


def send_facebook_text(
	*,
	conversation_name: str,
	text: str,
	client_nonce: str,
) -> dict:
	"""Send an outbound text message via the connector and persist delivery state.

	Same ``client_nonce`` + text + conversation always maps to one idempotency key.
	Repeated calls return the existing row and never issue a second Meta request.
	Timeouts / unreachable connector are stored as ``Unknown`` (not Failed).
	"""
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Message text is required"))

	conv = frappe.get_doc("Omnichannel Conversation", conversation_name)
	if conv.provider != "Facebook":
		frappe.throw(_("Conversation provider does not support Facebook send"))

	idem = build_outbound_idempotency_key(
		conversation_name=conv.name,
		text=text,
		client_nonce=client_nonce,
	)

	existing = frappe.db.get_value(
		"Omnichannel Message",
		{"idempotency_key": idem},
		["name", "delivery_status", "error_summary"],
		as_dict=True,
	)
	if existing:
		# Prevent duplicate sends for the same client action.
		return _response_for_existing(existing, deduplicated=True)

	msg = frappe.get_doc(
		{
			"doctype": "Omnichannel Message",
			"conversation": conv.name,
			"channel": conv.channel,
			"provider": "Facebook",
			"direction": "Outbound",
			"message_type": "Text",
			"text_content": text,
			"content": text,
			"idempotency_key": idem,
			"delivery_status": "Queued",
			"sent_at": now_datetime(),
			"sender_external_id": conv.page_id,
		}
	)
	try:
		msg.insert(ignore_permissions=True)
	except Exception as exc:
		if not _is_duplicate_idempotency_error(exc):
			raise
		# Roll back only this failed insert (no prior writes in this function).
		# The winner's committed row is untouched; we never call Meta here.
		frappe.db.rollback()
		existing = frappe.db.get_value(
			"Omnichannel Message",
			{"idempotency_key": idem},
			["name", "delivery_status", "error_summary"],
			as_dict=True,
		)
		if not existing:
			raise
		return _response_for_existing(existing, deduplicated=True)

	frappe.db.commit()

	try:
		adapter = FacebookConnectorAdapter()
		result = adapter.send_text(
			page_id=conv.page_id,
			recipient_id=conv.participant_id,
			text=text,
			idempotency_key=idem,
		)
		external_id = None
		if isinstance(result, dict):
			external_id = (
				result.get("message_id")
				or result.get("id")
				or result.get("mid")
				or result.get("external_message_id")
			)
		values = {"delivery_status": "Sent"}
		if external_id:
			# Only set if unique
			if not frappe.db.exists("Omnichannel Message", {"external_message_id": external_id}):
				values["external_message_id"] = str(external_id)
		frappe.db.set_value("Omnichannel Message", msg.name, values)
		frappe.db.set_value(
			"Omnichannel Conversation",
			conv.name,
			{
				"last_message_at": now_datetime(),
				"last_message_preview": text[:500],
			},
		)
		return {
			"ok": True,
			"uncertain": False,
			"message": msg.name,
			"delivery_status": "Sent",
			"deduplicated": False,
		}
	except _UNCERTAIN_EXCEPTIONS as exc:
		# Timeout / unreachable / HTTP 5xx — Meta may already have the message.
		# Never mark Failed; never auto-retry.
		frappe.db.set_value(
			"Omnichannel Message",
			msg.name,
			{"delivery_status": "Unknown", "error_summary": str(exc)[:440]},
		)
		return {
			"ok": False,
			"uncertain": True,
			"message": msg.name,
			"delivery_status": "Unknown",
			"error": str(exc),
			"deduplicated": False,
		}
	except ConnectorError as exc:
		# Auth / forbidden / rate-limit / unexpected 4xx — non-delivery established.
		frappe.db.set_value(
			"Omnichannel Message",
			msg.name,
			{"delivery_status": "Failed", "error_summary": str(exc)[:440]},
		)
		return {
			"ok": False,
			"uncertain": False,
			"message": msg.name,
			"delivery_status": "Failed",
			"error": str(exc),
			"deduplicated": False,
		}

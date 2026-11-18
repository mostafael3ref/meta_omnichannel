"""Outbound Facebook text sending with local persistence and idempotency."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from meta_omnichannel.facebook_messenger.adapter import FacebookConnectorAdapter
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.utils.idempotency import build_outbound_idempotency_key


def send_facebook_text(
	*,
	conversation_name: str,
	text: str,
	client_nonce: str,
) -> dict:
	"""Send an outbound text message via the connector and persist delivery state."""
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

	existing = frappe.db.get_value("Omnichannel Message", {"idempotency_key": idem}, ["name", "delivery_status"], as_dict=True)
	if existing:
		# Prevent duplicate sends for the same client action.
		return {
			"ok": existing.delivery_status in ("Sent", "Delivered", "Read", "Queued"),
			"message": existing.name,
			"delivery_status": existing.delivery_status,
			"deduplicated": True,
		}

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
	msg.insert(ignore_permissions=True)
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
			"message": msg.name,
			"delivery_status": "Sent",
			"deduplicated": False,
		}
	except ConnectorError as exc:
		frappe.db.set_value(
			"Omnichannel Message",
			msg.name,
			{"delivery_status": "Failed", "error_summary": str(exc)[:440]},
		)
		return {
			"ok": False,
			"message": msg.name,
			"delivery_status": "Failed",
			"error": str(exc),
			"deduplicated": False,
		}

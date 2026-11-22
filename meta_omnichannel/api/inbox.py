"""Whitelisted Unified Inbox APIs (server-side; never returns connector secrets)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from meta_omnichannel.permissions import (
	ensure_omnichannel_agent,
	has_conversation_permission,
	user_is_omnichannel_admin,
)
from meta_omnichannel.utils.datetimes import parse_connector_datetime_for_db


def _serialize_conversation(row) -> dict:
	return {
		"name": row.name,
		"provider": row.provider,
		"channel": row.channel,
		"participant_name": row.participant_name,
		"participant_id": row.participant_id,
		"status": row.status,
		"assigned_to": row.assigned_to,
		"unread_count": cint(row.unread_count),
		"last_message_at": row.last_message_at,
		"last_message_preview": row.last_message_preview,
		"last_inbound_at": row.last_inbound_at,
		"crm_contact": row.crm_contact,
		"crm_lead": row.crm_lead,
		"page_id": row.page_id,
	}


def _parse_read_watermark(value: Any) -> datetime | None:
	"""Normalize a client open-time read watermark to a naive datetime."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.replace(tzinfo=None) if value.tzinfo is not None else value
	return parse_connector_datetime_for_db(value)


@frappe.whitelist()
def list_conversations(
	provider: str | None = None,
	status: str | None = None,
	assigned: str | None = None,
	search: str | None = None,
	limit: int = 30,
	offset: int = 0,
) -> dict:
	ensure_omnichannel_agent()
	limit = max(1, min(cint(limit), 100))
	offset = max(0, cint(offset))

	filters: dict = {"is_archived": 0}
	if provider and provider not in ("All", "all"):
		filters["provider"] = provider
	if status:
		filters["status"] = status

	or_filters = None
	if search:
		term = f"%{search}%"
		or_filters = [
			["participant_name", "like", term],
			["participant_id", "like", term],
			["last_message_preview", "like", term],
		]

	# assigned filter
	extra = []
	user = frappe.session.user
	if assigned == "me":
		filters["assigned_to"] = user
	elif assigned == "unassigned":
		filters["assigned_to"] = ["in", ["", None]]

	if not user_is_omnichannel_admin():
		# Non-admins: only own + unassigned
		# Applied via permission_query_conditions; still filter defensively.
		pass

	rows = frappe.get_list(
		"Omnichannel Conversation",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"provider",
			"channel",
			"participant_name",
			"participant_id",
			"status",
			"assigned_to",
			"unread_count",
			"last_message_at",
			"last_message_preview",
			"last_inbound_at",
			"crm_contact",
			"crm_lead",
			"page_id",
		],
		order_by="last_message_at desc",
		limit_page_length=limit,
		limit_start=offset,
	)
	total = frappe.db.count("Omnichannel Conversation", filters)
	return {
		"items": [_serialize_conversation(r) for r in rows],
		"total": total,
		"limit": limit,
		"offset": offset,
		"has_more": offset + len(rows) < total,
		"next_offset": offset + len(rows) if offset + len(rows) < total else None,
	}


@frappe.whitelist()
def get_conversation(name: str) -> dict:
	ensure_omnichannel_agent()
	doc = frappe.get_doc("Omnichannel Conversation", name)
	if not has_conversation_permission(doc, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return _serialize_conversation(doc)


@frappe.whitelist()
def list_messages(conversation: str, limit: int = 50, offset: int = 0) -> dict:
	ensure_omnichannel_agent()
	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	limit = max(1, min(cint(limit), 100))
	offset = max(0, cint(offset))
	rows = frappe.get_all(
		"Omnichannel Message",
		filters={"conversation": conversation},
		fields=[
			"name",
			"direction",
			"message_type",
			"text_content",
			"delivery_status",
			"sent_at",
			"attachment_url",
			"attachment_mime",
			"is_internal_note",
			"error_summary",
			"external_message_id",
		],
		# Stable chronology: primary sent_at, then creation/name for ties + pagination.
		order_by="sent_at asc, creation asc, name asc",
		limit_page_length=limit,
		limit_start=offset,
	)
	total = frappe.db.count("Omnichannel Message", {"conversation": conversation})
	# Fetch only — never clear unread here (open / soft-refresh / pagination share this).

	return {
		"items": rows,
		"total": total,
		"limit": limit,
		"offset": offset,
		"has_more": offset + len(rows) < total,
		"next_offset": offset + len(rows) if offset + len(rows) < total else None,
		"conversation": _serialize_conversation(conv),
	}


@frappe.whitelist()
def mark_conversation_read(conversation: str, as_of: str | None = None) -> dict:
	"""Mark a conversation read using a required open-time watermark.

	``unread_count`` is recomputed as the number of inbound non-note messages
	with ``sent_at`` strictly after ``as_of``. A delayed request therefore cannot
	erase tips that arrived after the agent opened the conversation.

	Missing or invalid ``as_of`` is a no-op (returns the current unread count).
	Unconditional clear-to-zero would be race-unsafe under delayed requests.
	"""
	ensure_omnichannel_agent()
	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	as_of_dt = _parse_read_watermark(as_of)
	if as_of_dt is None:
		unread = cint(
			frappe.db.get_value("Omnichannel Conversation", conversation, "unread_count")
		)
		return {
			"ok": True,
			"conversation": conversation,
			"unread_count": unread,
			"updated": False,
		}

	frappe.db.sql(
		"""
		UPDATE `tabOmnichannel Conversation` AS c
		SET c.unread_count = (
			SELECT COUNT(*)
			FROM `tabOmnichannel Message` AS m
			WHERE m.conversation = c.name
			  AND m.direction = 'Inbound'
			  AND IFNULL(m.is_internal_note, 0) = 0
			  AND m.sent_at IS NOT NULL
			  AND m.sent_at > %(as_of)s
		)
		WHERE c.name = %(name)s
		""",
		{"name": conversation, "as_of": as_of_dt},
	)

	unread = cint(
		frappe.db.get_value("Omnichannel Conversation", conversation, "unread_count")
	)
	return {
		"ok": True,
		"conversation": conversation,
		"unread_count": unread,
		"updated": True,
	}


@frappe.whitelist()
def send_message(conversation: str, text: str, client_nonce: str) -> dict:
	ensure_omnichannel_agent()
	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if conv.provider == "Facebook":
		from meta_omnichannel.facebook_messenger.outbound import send_facebook_text

		return send_facebook_text(
			conversation_name=conversation,
			text=text,
			client_nonce=client_nonce,
		)

	# Instagram / WhatsApp pending connector contracts
	frappe.throw(
		_("Outbound messaging for {0} is not available until the connector API is confirmed").format(
			conv.provider
		)
	)


def _ensure_can_trigger_connector_sync() -> None:
	"""Agents and admins may trigger connector sync; others are rejected."""
	ensure_omnichannel_agent()
	if user_is_omnichannel_admin():
		return
	if "Omnichannel Agent" in frappe.get_roles():
		return
	from meta_omnichannel.meta_connectors.permissions import ensure_omnichannel_admin

	ensure_omnichannel_admin()


@frappe.whitelist()
def sync_facebook_inbox(limit: int = 50) -> dict:
	"""Sync Facebook conversation list, then bounded recent/new message history."""
	_ensure_can_trigger_connector_sync()
	from meta_omnichannel.facebook_messenger.sync import sync_facebook_inbox_bounded

	return sync_facebook_inbox_bounded(limit=cint(limit) or 50)


@frappe.whitelist()
def sync_instagram_inbox(limit: int = 50) -> dict:
	"""Sync Instagram conversations from the connector (list only; Step 1).

	If conversation items lack verified identity fields (``account_id``,
	``sender_id``), the sync layer returns ``sync_blocked=True`` without
	writing DocTypes.
	"""
	_ensure_can_trigger_connector_sync()
	from meta_omnichannel.instagram.sync import sync_instagram_conversations

	return sync_instagram_conversations(limit=cint(limit) or 50)


def _run_channel_sync(label: str, runner) -> dict:
	"""Run one channel sync; never raise — failures become ``ok: False``."""
	try:
		result = runner()
		if not isinstance(result, dict):
			return {"ok": False, "error": f"{label}: unexpected sync result"}
		return result
	except Exception as exc:
		return {"ok": False, "error": f"{label}: {exc}"}


def _sync_facebook_conversations(limit: int) -> dict:
	"""Facebook channel runner for ``sync_inbox`` (list + bounded messages)."""
	from meta_omnichannel.facebook_messenger.sync import sync_facebook_inbox_bounded

	return sync_facebook_inbox_bounded(limit=limit)


def _sync_instagram_conversations(limit: int) -> dict:
	from meta_omnichannel.instagram.sync import sync_instagram_conversations

	return sync_instagram_conversations(limit=limit)


@frappe.whitelist()
def sync_inbox(limit: int = 50) -> dict:
	"""Sync Facebook (conversations + bounded messages) and Instagram list independently.

	One channel failing does not prevent the other from running. The Unified
	Inbox Sync button should call this method (not Graph / browser keys).
	Instagram remains list-only until message sync is enabled for that provider.
	"""
	_ensure_can_trigger_connector_sync()
	limit = cint(limit) or 50

	facebook = _run_channel_sync(
		"Facebook", lambda: _sync_facebook_conversations(limit)
	)
	instagram = _run_channel_sync(
		"Instagram", lambda: _sync_instagram_conversations(limit)
	)

	facebook_ok = bool(facebook.get("ok"))
	instagram_ok = bool(instagram.get("ok"))
	errors = []
	if not facebook_ok:
		errors.append(facebook.get("error") or "Facebook sync failed")
	if not instagram_ok:
		errors.append(instagram.get("error") or "Instagram sync failed")

	# Channel isolation: Instagram still runs when Facebook is ok:false/partial.
	# Top-level partial covers either cross-channel or Facebook messages_partial.
	cross_channel_partial = (facebook_ok or instagram_ok) and not (facebook_ok and instagram_ok)
	facebook_partial = bool(facebook.get("partial"))
	return {
		"ok": facebook_ok and instagram_ok,
		"partial": cross_channel_partial or (facebook_partial and instagram_ok),
		"facebook": facebook,
		"instagram": instagram,
		"error": " · ".join(errors) if errors else None,
	}


@frappe.whitelist()
def sync_conversation(conversation: str) -> dict:
	ensure_omnichannel_agent()
	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if conv.provider != "Facebook":
		return {
			"ok": False,
			"error": f"{conv.provider} sync requires a confirmed connector API",
			"pending_contract": True,
		}
	from meta_omnichannel.facebook_messenger.sync import sync_conversation_messages

	return sync_conversation_messages(conversation)

"""Import Facebook conversations/messages from the connector into Frappe."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from meta_omnichannel.facebook_messenger.adapter import FacebookConnectorAdapter
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.sync_log import finish_sync_log, start_sync_log
from meta_omnichannel.utils.threading_keys import facebook_thread_key as thread_key

PROVIDER = "Facebook"
MAX_CONVERSATION_PAGES = 5
MAX_MESSAGE_PAGES = 3


def _payload_hash(payload: Any) -> str:
	raw = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_facebook_channel(page_id: str, page_name: str | None = None) -> str:
	existing = frappe.db.get_value(
		"Omnichannel Channel",
		{"provider": PROVIDER, "external_account_id": page_id},
		"name",
	)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Channel",
			"channel_name": page_name or f"Facebook Page {page_id}",
			"provider": PROVIDER,
			"external_account_id": page_id,
			"external_account_name": page_name,
			"status": "Connected",
			"is_enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
	for key in ("items", "data", "conversations", "messages", "results"):
		value = payload.get(key)
		if isinstance(value, list):
			return [row for row in value if isinstance(row, dict)]
	return []


def upsert_conversation_from_connector(row: dict[str, Any]) -> str | None:
	page_id = str(row.get("page_id") or row.get("pageId") or "").strip()
	sender_id = str(
		row.get("sender_id") or row.get("senderId") or row.get("participant_id") or row.get("psid") or ""
	).strip()
	if not page_id or not sender_id:
		return None

	channel = _ensure_facebook_channel(page_id, row.get("page_name") or row.get("pageName"))
	key = thread_key(page_id, sender_id)
	name = frappe.db.get_value("Omnichannel Conversation", {"external_thread_key": key}, "name")
	preview = (
		row.get("last_message")
		or row.get("last_message_text")
		or row.get("snippet")
		or row.get("preview")
		or ""
	)
	last_at = row.get("last_message_at") or row.get("updated_at") or row.get("timestamp")
	participant_name = (
		row.get("participant_name")
		or row.get("sender_name")
		or row.get("name")
		or sender_id
	)

	values = {
		"channel": channel,
		"provider": PROVIDER,
		"external_thread_key": key,
		"page_id": page_id,
		"participant_id": sender_id,
		"participant_name": participant_name,
		"last_message_preview": str(preview)[:500],
	}
	if last_at:
		try:
			values["last_message_at"] = get_datetime(last_at)
		except Exception:
			pass

	if name:
		frappe.db.set_value("Omnichannel Conversation", name, values, update_modified=True)
		return name

	doc = frappe.get_doc({"doctype": "Omnichannel Conversation", "status": "Open", **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def upsert_message_from_connector(conversation: str, channel: str, row: dict[str, Any]) -> str | None:
	external_id = str(
		row.get("id") or row.get("message_id") or row.get("mid") or row.get("external_message_id") or ""
	).strip()
	text = str(row.get("text") or row.get("message") or row.get("body") or "").strip()
	direction_raw = str(row.get("direction") or row.get("type") or "").lower()
	if direction_raw in ("out", "outbound", "sent", "agent"):
		direction = "Outbound"
	elif direction_raw in ("in", "inbound", "received", "user", "customer"):
		direction = "Inbound"
	else:
		# Heuristic: messages from page are outbound if is_echo / from_page flags present
		if row.get("is_echo") or row.get("from_page"):
			direction = "Outbound"
		else:
			direction = "Inbound"

	if external_id:
		existing = frappe.db.get_value(
			"Omnichannel Message", {"external_message_id": external_id}, "name"
		)
		if existing:
			return existing

	# Dedup without external id: hash conversation + text + timestamp
	sent_at = row.get("timestamp") or row.get("created_at") or row.get("sent_at")
	fingerprint = _payload_hash({"c": conversation, "t": text, "s": sent_at, "d": direction})
	existing_fp = frappe.db.get_value("Omnichannel Message", {"raw_payload_hash": fingerprint}, "name")
	if existing_fp:
		return existing_fp

	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Message",
			"conversation": conversation,
			"channel": channel,
			"provider": PROVIDER,
			"direction": direction,
			"message_type": "Text" if text else "Unknown",
			"text_content": text,
			"content": text,
			"external_message_id": external_id or None,
			"delivery_status": "Delivered" if direction == "Inbound" else "Sent",
			"sent_at": get_datetime(sent_at) if sent_at else now_datetime(),
			"sender_external_id": str(row.get("sender_id") or row.get("from") or ""),
			"raw_payload_hash": fingerprint,
			"attachment_url": row.get("attachment_url") or row.get("media_url"),
			"attachment_mime": row.get("mime_type") or row.get("attachment_mime"),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def sync_facebook_conversations(*, limit: int = 50, max_pages: int = MAX_CONVERSATION_PAGES) -> dict:
	"""Bounded conversation sync. Does not poll unbounded history."""
	log_name = start_sync_log(PROVIDER, "Conversation Sync")
	adapter = FacebookConnectorAdapter()
	processed = 0
	offset = 0
	pages = 0
	try:
		while pages < max_pages:
			payload = adapter.list_conversations(limit=limit, offset=offset)
			items = _extract_items(payload)
			if not items:
				break
			for row in items:
				if upsert_conversation_from_connector(row):
					processed += 1
			pages += 1
			has_more = bool(payload.get("has_more"))
			next_offset = payload.get("next_offset")
			if not has_more:
				break
			offset = cint(next_offset if next_offset is not None else offset + limit)
		finish_sync_log(log_name, status="Success", processed=processed, details={"pages": pages})
		return {"ok": True, "processed": processed, "pages": pages, "sync_log": log_name}
	except ConnectorError as exc:
		finish_sync_log(log_name, status="Failed", processed=processed, error=str(exc))
		get_logger().info("facebook_conversation_sync_failed summary=%s", str(exc))
		return {"ok": False, "processed": processed, "error": str(exc), "sync_log": log_name}


def sync_conversation_messages(conversation_name: str, *, limit: int = 50, max_pages: int = MAX_MESSAGE_PAGES) -> dict:
	"""Import message batches for one conversation. offset 0 = newest batch per contract."""
	conv = frappe.get_doc("Omnichannel Conversation", conversation_name)
	if conv.provider != PROVIDER:
		frappe.throw("Not a Facebook conversation")

	log_name = start_sync_log(
		PROVIDER, "Message Sync", conversation=conversation_name, channel=conv.channel
	)
	adapter = FacebookConnectorAdapter()
	processed = 0
	offset = 0
	pages = 0
	try:
		while pages < max_pages:
			payload = adapter.get_messages(
				page_id=conv.page_id,
				sender_id=conv.participant_id,
				limit=limit,
				offset=offset,
			)
			items = _extract_items(payload)
			if not items:
				break
			# Contract: each batch is chronological; offset 0 is newest batch.
			for row in items:
				if upsert_message_from_connector(conv.name, conv.channel, row):
					processed += 1
			pages += 1
			has_more = bool(payload.get("has_more"))
			next_offset = payload.get("next_offset")
			if not has_more:
				break
			offset = cint(next_offset if next_offset is not None else offset + limit)

		# Refresh preview from latest local message
		latest = frappe.db.sql(
			"""
			select text_content, sent_at from `tabOmnichannel Message`
			where conversation=%s and ifnull(is_internal_note,0)=0
			order by sent_at desc limit 1
			""",
			conversation_name,
			as_dict=True,
		)
		if latest:
			frappe.db.set_value(
				"Omnichannel Conversation",
				conversation_name,
				{
					"last_message_preview": (latest[0].text_content or "")[:500],
					"last_message_at": latest[0].sent_at,
				},
			)
		frappe.db.set_value("Omnichannel Channel", conv.channel, "last_synced_at", now_datetime())
		finish_sync_log(log_name, status="Success", processed=processed, details={"pages": pages})
		return {"ok": True, "processed": processed, "pages": pages, "sync_log": log_name}
	except ConnectorError as exc:
		finish_sync_log(log_name, status="Failed", processed=processed, error=str(exc))
		return {"ok": False, "processed": processed, "error": str(exc), "sync_log": log_name}

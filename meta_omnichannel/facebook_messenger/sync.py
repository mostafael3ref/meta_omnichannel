"""Import Facebook conversations/messages from the connector into Frappe."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from meta_omnichannel.facebook_messenger.adapter import FacebookConnectorAdapter
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.utils.datetimes import (
	connector_timestamp_is_present,
	parse_connector_datetime_for_db,
)
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.sync_log import finish_sync_log, start_sync_log
from meta_omnichannel.utils.threading_keys import facebook_thread_key as thread_key

PROVIDER = "Facebook"
MAX_CONVERSATION_PAGES = 5
MAX_MESSAGE_PAGES = 3

# Connector attachment ``type`` → Omnichannel Message.message_type
_ATTACHMENT_TYPE_MAP = {
	"image": "Image",
	"audio": "Audio",
	"video": "Video",
	"file": "File",
}


def _payload_hash(payload: Any) -> str:
	raw = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(raw.encode()).hexdigest()


def extract_connector_attachments(row: dict[str, Any]) -> list[dict[str, str | None]]:
	"""Normalize connector attachment payloads into ``{type, url, mime}`` dicts.

	Supports the confirmed ``attachments: [{type, url}]`` list plus legacy
	top-level ``attachment_url`` / ``media_url``. Malformed entries are skipped.
	MIME is copied only when the connector supplies a value — never invented.
	"""
	found: list[dict[str, str | None]] = []
	seen_urls: set[str] = set()

	raw_list = row.get("attachments")
	if isinstance(raw_list, list):
		for item in raw_list:
			if not isinstance(item, dict):
				continue
			url = str(item.get("url") or "").strip()
			if not url or url in seen_urls:
				continue
			atype = str(item.get("type") or "").strip().lower()
			mime_raw = item.get("mime_type") or item.get("mime") or item.get("attachment_mime")
			mime = str(mime_raw).strip() if mime_raw else None
			found.append({"type": atype or None, "url": url, "mime": mime or None})
			seen_urls.add(url)

	top_url = str(row.get("attachment_url") or row.get("media_url") or "").strip()
	if top_url and top_url not in seen_urls:
		top_type = str(row.get("attachment_type") or row.get("media_type") or "").strip().lower()
		mime_raw = row.get("mime_type") or row.get("attachment_mime")
		mime = str(mime_raw).strip() if mime_raw else None
		# Prefer top-level URL as primary when no list attachments were found.
		entry = {"type": top_type or None, "url": top_url, "mime": mime or None}
		if found:
			found.append(entry)
		else:
			found.insert(0, entry)
		seen_urls.add(top_url)

	return found


def resolve_message_type_and_attachment(
	*, text: str, attachments: list[dict[str, str | None]]
) -> tuple[str, str | None, str | None]:
	"""Return ``(message_type, attachment_url, attachment_mime)``.

	Schema limitation: Omnichannel Message has a single ``attachment_url``.
	Only the **primary** (first valid) attachment is persisted. Additional
	attachments are recognized for dedup hashing but are **not** stored and are
	**not** written into ``text_content`` / ``content`` (Text Editor must not
	receive untrusted URLs/HTML). This is not full multi-attachment support.
	"""
	primary = attachments[0] if attachments else None
	attachment_url = primary["url"] if primary else None
	attachment_mime = primary.get("mime") if primary else None

	if primary:
		mapped = _ATTACHMENT_TYPE_MAP.get((primary.get("type") or "").lower())
		if mapped:
			message_type = mapped
		elif text:
			message_type = "Text"
		else:
			message_type = "Unknown"
	else:
		message_type = "Text" if text else "Unknown"

	return message_type, attachment_url, attachment_mime


def build_message_dedup_fingerprint(
	*,
	conversation: str,
	text: str,
	sent_at: Any,
	direction: str,
	attachments: list[dict[str, str | None]],
) -> str:
	"""Build ``raw_payload_hash`` for messages without an external id.

	Compatibility strategy (narrow / fail-safe):

	- **No attachments:** exact legacy payload ``{c, t, s, d}`` so existing
	  text-only rows keep the same fingerprint.
	- **With attachments:** include ``a`` (type, url) tuples so distinct
	  attachment-only rows do not collide on empty text.
	- **Do not** fall back to the legacy text-only fingerprint when attachments
	  are present — that would incorrectly merge different media that share the
	  same conversation / empty text / timestamp / direction.

	Limitation: pre-change attachment-only rows stored under the legacy empty-text
	hash (no ``a``) cannot be reliably matched without schema metadata; re-sync
	may insert a second row rather than guessing a merge.
	"""
	fp_payload: dict[str, Any] = {
		"c": conversation,
		"t": text,
		"s": sent_at,
		"d": direction,
	}
	if attachments:
		fp_payload["a"] = [(a.get("type"), a.get("url")) for a in attachments]
	return _payload_hash(fp_payload)


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
	last_at_raw = row.get("last_message_at") or row.get("updated_at") or row.get("timestamp")
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
	# Only set when parse succeeds — never write NULL / invent "now" for bad values.
	parsed_last_at = parse_connector_datetime_for_db(last_at_raw)
	if parsed_last_at is not None:
		values["last_message_at"] = parsed_last_at

	if name:
		frappe.db.set_value("Omnichannel Conversation", name, values, update_modified=True)
		return name

	doc = frappe.get_doc({"doctype": "Omnichannel Conversation", "status": "Open", **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def _raw_message_timestamp(row: dict[str, Any]) -> Any:
	"""First non-empty connector timestamp candidate for a message row."""
	for key in ("timestamp", "created_at", "sent_at"):
		value = row.get(key)
		if connector_timestamp_is_present(value):
			return value
	return None


def _resolve_new_message_sent_at(raw_timestamp: Any):
	"""Map connector timestamp → DB ``sent_at`` for a new message insert.

	Policy:
	- Valid connector timestamp → system-timezone naive datetime.
	- Genuinely absent timestamp → ``now_datetime()`` (same as prior fallback).
	- Present but invalid → ``None`` (never invent "now" for bad values).
	"""
	if not connector_timestamp_is_present(raw_timestamp):
		return now_datetime()
	parsed = parse_connector_datetime_for_db(raw_timestamp)
	return parsed  # may be None when invalid


def _maybe_repair_null_sent_at(message_name: str, current_sent_at: Any, raw_timestamp: Any) -> None:
	"""Fill ``sent_at`` only when currently NULL and connector time parses cleanly."""
	if current_sent_at is not None:
		return
	parsed = parse_connector_datetime_for_db(raw_timestamp)
	if parsed is None:
		return
	frappe.db.set_value(
		"Omnichannel Message",
		message_name,
		"sent_at",
		parsed,
		update_modified=False,
	)


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

	attachments = extract_connector_attachments(row)
	message_type, attachment_url, attachment_mime = resolve_message_type_and_attachment(
		text=text, attachments=attachments
	)
	# Customer text only — never splice attachment URLs/HTML into Text Editor content.
	text_content = text
	raw_timestamp = _raw_message_timestamp(row)

	if external_id:
		# Match conversation + external id so repair never crosses threads.
		existing = frappe.db.get_value(
			"Omnichannel Message",
			{"external_message_id": external_id, "conversation": conversation},
			["name", "sent_at"],
			as_dict=True,
		)
		if existing:
			_maybe_repair_null_sent_at(existing.name, existing.sent_at, raw_timestamp)
			return existing.name
		# Unique external_message_id elsewhere: never insert a duplicate.
		elsewhere = frappe.db.get_value(
			"Omnichannel Message", {"external_message_id": external_id}, "name"
		)
		if elsewhere:
			return elsewhere

	fingerprint = build_message_dedup_fingerprint(
		conversation=conversation,
		text=text,
		sent_at=raw_timestamp,
		direction=direction,
		attachments=attachments,
	)
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
			"message_type": message_type,
			"text_content": text_content,
			"content": text_content,
			"external_message_id": external_id or None,
			"delivery_status": "Delivered" if direction == "Inbound" else "Sent",
			"sent_at": _resolve_new_message_sent_at(raw_timestamp),
			"sender_external_id": str(row.get("sender_id") or row.get("from") or ""),
			"raw_payload_hash": fingerprint,
			"attachment_url": attachment_url,
			"attachment_mime": attachment_mime,
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

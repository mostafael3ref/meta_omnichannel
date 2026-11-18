"""Import Instagram conversations from the connector into Frappe DocTypes.

Required identity fields: ``account_id`` and ``sender_id``.
Optional display fields: ``last_message``, ``last_message_at``.

If required identity fields are absent on conversation items, sync is blocked
(fail closed) instead of guessing aliases (page_id, psid, etc.).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from meta_omnichannel.instagram.adapter import (
	InstagramConnectorAdapter,
	conversation_items_ready_for_sync,
)
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.utils.datetimes import parse_connector_datetime
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.sync_log import finish_sync_log, start_sync_log
from meta_omnichannel.utils.threading_keys import instagram_thread_key

PROVIDER = "Instagram"
MAX_CONVERSATION_PAGES = 5
PREVIEW_MAX_LEN = 500


def _ensure_instagram_channel(account_id: str) -> str:
	existing = frappe.db.get_value(
		"Omnichannel Channel",
		{"provider": PROVIDER, "external_account_id": account_id},
		"name",
	)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Channel",
			"channel_name": f"Instagram Account {account_id}",
			"provider": PROVIDER,
			"external_account_id": account_id,
			"status": "Connected",
			"is_enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _optional_preview_and_timestamp(row: dict[str, Any]) -> dict[str, Any]:
	"""Map confirmed optional list fields without overwriting with empties/invalids."""
	optional: dict[str, Any] = {}
	preview_raw = row.get("last_message")
	if preview_raw is not None:
		preview = str(preview_raw).strip()
		if preview:
			optional["last_message_preview"] = preview[:PREVIEW_MAX_LEN]

	parsed_at = parse_connector_datetime(row.get("last_message_at"))
	if parsed_at is not None:
		optional["last_message_at"] = parsed_at
	return optional


def upsert_instagram_conversation(row: dict[str, Any]) -> str | None:
	"""Upsert one conversation using verified identity + optional preview fields.

	Returns None when required identity fields are missing (caller should have
	blocked the batch already; this is defense in depth).
	"""
	account_id = str(row.get("account_id") or "").strip()
	sender_id = str(row.get("sender_id") or "").strip()
	if not account_id or not sender_id:
		return None

	channel = _ensure_instagram_channel(account_id)
	key = instagram_thread_key(account_id, sender_id)
	name = frappe.db.get_value("Omnichannel Conversation", {"external_thread_key": key}, "name")

	values = {
		"channel": channel,
		"provider": PROVIDER,
		"external_thread_key": key,
		# DocType field is named page_id; stores the Instagram account id.
		"page_id": account_id,
		"participant_id": sender_id,
		"participant_name": sender_id,
	}
	values.update(_optional_preview_and_timestamp(row))

	if name:
		frappe.db.set_value("Omnichannel Conversation", name, values, update_modified=True)
		return name

	doc = frappe.get_doc({"doctype": "Omnichannel Conversation", "status": "Open", **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def sync_instagram_conversations(*, limit: int = 50, max_pages: int = MAX_CONVERSATION_PAGES) -> dict:
	"""Bounded Instagram conversation list sync.

	Fail-closed: if item schema lacks ``account_id`` / ``sender_id``, no DocType
	writes occur for that (or subsequent) page.
	"""
	log_name = start_sync_log(PROVIDER, "Conversation Sync")
	adapter = InstagramConnectorAdapter()
	processed = 0
	offset = 0
	pages = 0
	try:
		while pages < max_pages:
			payload = adapter.list_conversations(limit=limit, offset=offset)
			ready, missing = conversation_items_ready_for_sync(payload)
			if not ready:
				message = (
					"Instagram conversation item schema insufficient for sync. "
					"Missing required item fields: " + ", ".join(missing)
				)
				finish_sync_log(
					log_name,
					status="Failed",
					processed=processed,
					error=message,
					details={
						"sync_blocked": True,
						"missing_item_fields": missing,
						"envelope_ok": True,
						"pages": pages,
						"total": payload.get("total"),
					},
				)
				get_logger().info("instagram_conversation_sync_blocked missing=%s", missing)
				return {
					"ok": False,
					"sync_blocked": True,
					"processed": processed,
					"error": message,
					"missing_item_fields": missing,
					"envelope_ok": True,
					"total": payload.get("total"),
					"limit": payload.get("limit"),
					"offset": payload.get("offset"),
					"has_more": payload.get("has_more"),
					"next_offset": payload.get("next_offset"),
					"sync_log": log_name,
				}

			items = payload.get("conversations") or []
			if not items:
				break
			for row in items:
				if upsert_instagram_conversation(row):
					processed += 1
			pages += 1
			has_more = bool(payload.get("has_more"))
			next_offset = payload.get("next_offset")
			if not has_more:
				break
			offset = cint(next_offset if next_offset is not None else offset + limit)

		finish_sync_log(
			log_name,
			status="Success",
			processed=processed,
			details={"pages": pages},
		)
		return {
			"ok": True,
			"processed": processed,
			"pages": pages,
			"sync_log": log_name,
		}
	except ConnectorError as exc:
		finish_sync_log(log_name, status="Failed", processed=processed, error=str(exc))
		get_logger().info("instagram_conversation_sync_failed summary=%s", str(exc))
		return {
			"ok": False,
			"processed": processed,
			"error": str(exc),
			"sync_log": log_name,
		}

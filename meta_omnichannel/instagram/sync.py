"""Import Instagram conversations/messages from the connector into Frappe DocTypes.

Required identity fields: ``account_id`` and ``sender_id``.
Optional display fields: ``last_message``, ``last_message_at``.

If required identity fields are absent on conversation items, sync is blocked
(fail closed) instead of guessing aliases (page_id, psid, etc.).

Message persistence (``upsert_instagram_message_from_connector``) writes only into
an **existing** Instagram conversation with matching account/sender/channel
identity. It stores text + attachment **metadata** only — no Facebook attachment
jobs, remote media downloads, or outbound sends.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from meta_omnichannel.facebook_messenger.sync import (
	build_message_dedup_fingerprint,
	extract_connector_attachments,
	resolve_message_type_and_attachment,
)
from meta_omnichannel.instagram.adapter import (
	InstagramConnectorAdapter,
	conversation_items_ready_for_sync,
)
from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.utils.datetimes import (
	connector_timestamp_is_present,
	parse_connector_datetime,
	parse_connector_datetime_for_db,
)
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.sync_log import finish_sync_log, start_sync_log
from meta_omnichannel.utils.threading_keys import instagram_thread_key

PROVIDER = "Instagram"
MAX_CONVERSATION_PAGES = 5
MAX_MESSAGE_PAGES = 3
# Bounded fan-out: after list sync, import messages for at most this many threads.
DEFAULT_MAX_MESSAGE_CONVERSATIONS = 20
INSTAGRAM_INBOX_SYNC_LOCK_KEY = "omnichannel:instagram_inbox_sync"
INSTAGRAM_INBOX_SYNC_LOCK_TTL_SEC = 900  # 15 minutes; covers worst-case bounded run
INSTAGRAM_FAIR_CURSOR_KEY = "omnichannel:instagram_message_sync_fair_cursor"
# Lua: delete lock only when ownership token still matches.
_RELEASE_LOCK_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""
PREVIEW_MAX_LEN = 500
# Omnichannel Message.external_message_id is Frappe Data (varchar 140).
EXTERNAL_MESSAGE_ID_MAX_LEN = 140
_LONG_ID_PREFIX = "ig:"


def normalize_instagram_external_message_id(raw_id: str) -> str:
	"""Fit Instagram connector ``message_id`` into Data(140) without truncation.

	- Empty → ``""`` (caller uses fingerprint dedup).
	- Length ≤ 140 → unchanged (never re-hash an already-normalized ``ig:…`` value
	  that is already ≤ 140).
	- Length > 140 → ``ig:`` + SHA-256 hex of the **original** string (67 chars).

	Call once before every dedup lookup and before persist. Never truncate.
	"""
	value = (raw_id or "").strip()
	if not value:
		return ""
	if len(value) <= EXTERNAL_MESSAGE_ID_MAX_LEN:
		return value
	digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
	return f"{_LONG_ID_PREFIX}{digest}"


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


def _resolve_instagram_direction(row: dict[str, Any]) -> str | None:
	"""Map confirmed Instagram ``direction`` values. Fail closed on unknowns."""
	direction_raw = str(row.get("direction") or "").strip().lower()
	if direction_raw in ("out", "outbound", "sent"):
		return "Outbound"
	if direction_raw in ("in", "inbound", "received"):
		return "Inbound"
	return None


def _raw_instagram_message_timestamp(row: dict[str, Any]) -> Any:
	"""Confirmed Instagram history field is ``timestamp``."""
	value = row.get("timestamp")
	if connector_timestamp_is_present(value):
		return value
	return None


def _resolve_new_message_sent_at(raw_timestamp: Any):
	"""Map connector timestamp → DB ``sent_at`` for a new message insert.

	Policy mirrors Facebook message sync:
	- Valid connector timestamp → system-timezone naive datetime.
	- Genuinely absent timestamp → ``now_datetime()``.
	- Present but invalid → ``None`` (never invent "now" for bad values).
	"""
	if not connector_timestamp_is_present(raw_timestamp):
		return now_datetime()
	return parse_connector_datetime_for_db(raw_timestamp)


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


def _advance_last_inbound_at(conversation: str, candidate: datetime) -> None:
	"""Monotonic SQL advance of ``last_inbound_at`` (never moves backwards)."""
	frappe.db.sql(
		"""
		UPDATE `tabOmnichannel Conversation`
		SET last_inbound_at = %(ts)s
		WHERE name = %(name)s
		  AND (last_inbound_at IS NULL OR last_inbound_at < %(ts)s)
		""",
		{"name": conversation, "ts": candidate},
	)


def _increment_unread_count(conversation: str) -> None:
	"""Atomic unread bump (safe under concurrent tip inserts)."""
	frappe.db.sql(
		"""
		UPDATE `tabOmnichannel Conversation`
		SET unread_count = IFNULL(unread_count, 0) + 1
		WHERE name = %(name)s
		""",
		{"name": conversation},
	)


def _apply_inbound_conversation_metadata(
	conversation: str,
	*,
	raw_timestamp: Any,
	count_unread: bool,
	unread_watermark: datetime | None,
) -> None:
	"""Update ``last_inbound_at`` / unread after a newly inserted inbound message.

	Unread increments require a pre-sync watermark and a strictly newer connector
	time. Dedup hits must not call this (no duplicate unread).
	"""
	if not connector_timestamp_is_present(raw_timestamp):
		return
	parsed = parse_connector_datetime_for_db(raw_timestamp)
	if parsed is None:
		return
	_advance_last_inbound_at(conversation, parsed)
	if count_unread and unread_watermark is not None and parsed > unread_watermark:
		_increment_unread_count(conversation)


def resolve_existing_instagram_conversation(
	*, account_id: str, sender_id: str
) -> dict[str, Any] | None:
	"""Load an existing Instagram conversation that matches account/sender/channel.

	Returns ``None`` (fail closed) when the conversation is missing, provider is
	wrong, page/participant identity does not match, or the linked channel is not
	an Instagram channel for ``account_id``. Does not create conversations.
	"""
	account_id = str(account_id or "").strip()
	sender_id = str(sender_id or "").strip()
	if not account_id or not sender_id:
		return None

	key = instagram_thread_key(account_id, sender_id)
	conv = frappe.db.get_value(
		"Omnichannel Conversation",
		{"external_thread_key": key},
		["name", "channel", "provider", "page_id", "participant_id"],
		as_dict=True,
	)
	if not conv:
		return None
	if str(conv.provider or "") != PROVIDER:
		return None
	if str(conv.page_id or "").strip() != account_id:
		return None
	if str(conv.participant_id or "").strip() != sender_id:
		return None

	channel_meta = frappe.db.get_value(
		"Omnichannel Channel",
		conv.channel,
		["name", "provider", "external_account_id"],
		as_dict=True,
	)
	if not channel_meta:
		return None
	if str(channel_meta.provider or "") != PROVIDER:
		return None
	if str(channel_meta.external_account_id or "").strip() != account_id:
		return None

	return conv


def upsert_instagram_message_from_connector(
	*,
	account_id: str,
	sender_id: str,
	row: dict[str, Any],
	count_unread: bool = False,
	unread_watermark: datetime | None = None,
) -> dict[str, str] | None:
	"""Persist one Instagram connector message into an existing conversation.

	Uses confirmed history fields: ``direction``, ``text``, ``attachments``,
	``timestamp``, ``message_id`` (``message_id`` / ``text`` may be null).

	Dedup:
	- ``external_message_id`` within the conversation (after
	  ``normalize_instagram_external_message_id``)
	- global unique ``external_message_id`` elsewhere (incl. other providers) —
	  return existing name; never insert a second row or bump unread
	- stable ``raw_payload_hash`` fingerprint when ``message_id`` is absent

	Returns ``{"name": ..., "status": "inserted"|"duplicate"}`` or ``None`` when
	identity/direction validation fails.

	Does **not** enqueue Facebook attachment jobs, download remote media, or send
	outbound messages. Attachment URLs/MIME are stored as metadata only.
	"""
	if not isinstance(row, dict):
		return None

	conv = resolve_existing_instagram_conversation(
		account_id=account_id, sender_id=sender_id
	)
	if not conv:
		return None

	direction = _resolve_instagram_direction(row)
	if direction is None:
		return None

	message_id_raw = row.get("message_id")
	if message_id_raw is None:
		raw_external_id = ""
	elif isinstance(message_id_raw, str):
		raw_external_id = message_id_raw.strip()
	else:
		# Malformed id type — fail closed (adapter should have rejected this).
		return None
	# Normalize once; use for all dedup lookups and the stored value.
	external_id = normalize_instagram_external_message_id(raw_external_id)

	text_raw = row.get("text")
	if text_raw is None:
		text = ""
	elif isinstance(text_raw, str):
		text = text_raw.strip()
	else:
		return None

	attachments = extract_connector_attachments(row)
	message_type, attachment_url, attachment_mime = resolve_message_type_and_attachment(
		text=text, attachments=attachments
	)
	raw_timestamp = _raw_instagram_message_timestamp(row)
	conversation = conv.name
	channel = conv.channel

	if external_id:
		existing = frappe.db.get_value(
			"Omnichannel Message",
			{"external_message_id": external_id, "conversation": conversation},
			["name", "sent_at"],
			as_dict=True,
		)
		if existing:
			_maybe_repair_null_sent_at(existing.name, existing.sent_at, raw_timestamp)
			return {"name": existing.name, "status": "duplicate"}
		# Unique external_message_id elsewhere (same or other provider): no insert.
		elsewhere = frappe.db.get_value(
			"Omnichannel Message", {"external_message_id": external_id}, "name"
		)
		if elsewhere:
			return {"name": elsewhere, "status": "duplicate"}

	fingerprint = build_message_dedup_fingerprint(
		conversation=conversation,
		text=text,
		sent_at=raw_timestamp,
		direction=direction,
		attachments=attachments,
	)
	existing_fp = frappe.db.get_value(
		"Omnichannel Message", {"raw_payload_hash": fingerprint}, "name"
	)
	if existing_fp:
		return {"name": existing_fp, "status": "duplicate"}

	if direction == "Inbound":
		sender_external_id = str(sender_id or "").strip()
	else:
		sender_external_id = str(account_id or "").strip()

	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Message",
			"conversation": conversation,
			"channel": channel,
			"provider": PROVIDER,
			"direction": direction,
			"message_type": message_type,
			"text_content": text,
			"content": text,
			"external_message_id": external_id or None,
			"delivery_status": "Delivered" if direction == "Inbound" else "Sent",
			"sent_at": _resolve_new_message_sent_at(raw_timestamp),
			"sender_external_id": sender_external_id,
			"raw_payload_hash": fingerprint,
			"attachment_url": attachment_url,
			"attachment_mime": attachment_mime,
		}
	)
	doc.insert(ignore_permissions=True)
	if direction == "Inbound":
		_apply_inbound_conversation_metadata(
			conversation,
			raw_timestamp=raw_timestamp,
			count_unread=count_unread,
			unread_watermark=unread_watermark,
		)
	return {"name": doc.name, "status": "inserted"}


def _as_compare_datetime(value: Any) -> datetime | None:
	"""Normalize a DB/connector datetime for ordering comparisons (naive)."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.replace(tzinfo=None) if value.tzinfo is not None else value
	return parse_connector_datetime_for_db(value)


def _instagram_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
	"""Return confirmed ``messages`` list items (objects only)."""
	raw = payload.get("messages")
	if not isinstance(raw, list):
		return []
	return [row for row in raw if isinstance(row, dict)]


def _resolve_next_message_offset(
	*,
	current_offset: int,
	limit: int,
	payload: dict[str, Any],
	seen_offsets: set[int],
) -> tuple[int | None, str | None]:
	"""Compute the next page offset or a rejection reason.

	Returns ``(next_offset, None)`` when pagination should continue,
	``(None, None)`` when pagination ends cleanly (``has_more`` false),
	or ``(None, reason)`` when the offset is repeated/invalid.
	"""
	if not bool(payload.get("has_more")):
		return None, None

	raw_next = payload.get("next_offset")
	if raw_next is None:
		candidate = current_offset + max(1, int(limit))
	else:
		try:
			candidate = int(raw_next)
		except (TypeError, ValueError):
			return None, "invalid_next_offset"

	if candidate < 0:
		return None, "invalid_next_offset"
	if candidate == current_offset:
		return None, "repeated_offset"
	if candidate in seen_offsets:
		return None, "repeated_offset"
	# Offset must advance forward (newer→older pages grow the offset).
	if candidate < current_offset:
		return None, "invalid_next_offset"
	return candidate, None


def sync_instagram_conversation_messages(
	conversation_name: str,
	*,
	limit: int = 50,
	max_pages: int = MAX_MESSAGE_PAGES,
) -> dict:
	"""Import message batches for one existing Instagram conversation.

	Contract:
	- ``offset=0`` is the newest page; each page's ``messages`` are chronological.
	- Pages are fetched newest→older (bounded by ``max_pages``), then imported
	  oldest→newest so unread/watermark advances correctly.
	- Repeated or invalid ``next_offset`` values stop pagination and mark partial.
	- Counts distinguish ``inserted`` vs ``duplicates``.

	Does not wire Sync buttons/APIs, Real-Time, or webhooks.
	"""
	conversation_name = (conversation_name or "").strip()
	if not conversation_name:
		return {
			"ok": False,
			"partial": False,
			"inserted": 0,
			"duplicates": 0,
			"skipped": 0,
			"pages": 0,
			"processed": 0,
			"error": "conversation_name is required",
		}

	try:
		conv_doc = frappe.get_doc("Omnichannel Conversation", conversation_name)
	except Exception:
		return {
			"ok": False,
			"partial": False,
			"inserted": 0,
			"duplicates": 0,
			"skipped": 0,
			"pages": 0,
			"processed": 0,
			"error": "conversation_not_found",
		}

	if str(getattr(conv_doc, "provider", "") or "") != PROVIDER:
		return {
			"ok": False,
			"partial": False,
			"inserted": 0,
			"duplicates": 0,
			"skipped": 0,
			"pages": 0,
			"processed": 0,
			"error": "not_an_instagram_conversation",
		}

	account_id = str(getattr(conv_doc, "page_id", "") or "").strip()
	sender_id = str(getattr(conv_doc, "participant_id", "") or "").strip()
	resolved = resolve_existing_instagram_conversation(
		account_id=account_id, sender_id=sender_id
	)
	if not resolved or str(resolved.name) != conversation_name:
		return {
			"ok": False,
			"partial": False,
			"inserted": 0,
			"duplicates": 0,
			"skipped": 0,
			"pages": 0,
			"processed": 0,
			"error": "identity_mismatch",
		}

	limit = max(1, min(int(limit), 100))
	max_pages = max(1, int(max_pages))

	prior_message_count = frappe.db.count(
		"Omnichannel Message", {"conversation": conversation_name}
	)
	meta = frappe.db.get_value(
		"Omnichannel Conversation",
		conversation_name,
		["last_inbound_at", "last_message_at"],
		as_dict=True,
	)
	unread_watermark = None
	if meta:
		unread_watermark = _as_compare_datetime(meta.last_inbound_at) or _as_compare_datetime(
			meta.last_message_at
		)
	count_unread = prior_message_count > 0

	log_name = start_sync_log(
		PROVIDER, "Message Sync", conversation=conversation_name, channel=resolved.channel
	)
	adapter = InstagramConnectorAdapter()

	page_batches: list[list[dict[str, Any]]] = []
	offset = 0
	seen_offsets: set[int] = {0}
	partial = False
	error: str | None = None

	try:
		while len(page_batches) < max_pages:
			payload = adapter.get_messages(
				account_id=account_id,
				sender_id=sender_id,
				limit=limit,
				offset=offset,
			)
			# Envelope identity must match the conversation when present.
			env_account = str(payload.get("account_id") or "").strip()
			env_sender = str(payload.get("sender_id") or "").strip()
			if env_account and env_account != account_id:
				partial = True
				error = "envelope_identity_mismatch"
				break
			if env_sender and env_sender != sender_id:
				partial = True
				error = "envelope_identity_mismatch"
				break

			items = _instagram_messages_from_payload(payload)
			if items:
				page_batches.append(items)
			elif not page_batches:
				# Empty first page — clean completion.
				break
			else:
				# Empty later page while has_more would be odd; treat as end.
				break

			next_offset, offset_error = _resolve_next_message_offset(
				current_offset=offset,
				limit=limit,
				payload=payload,
				seen_offsets=seen_offsets,
			)
			if offset_error:
				partial = True
				error = offset_error
				break
			if next_offset is None:
				break
			seen_offsets.add(next_offset)
			offset = next_offset
	except ConnectorError as exc:
		error = str(exc)
		partial = bool(page_batches)
		if not page_batches:
			finish_sync_log(log_name, status="Failed", processed=0, error=error)
			return {
				"ok": False,
				"partial": False,
				"inserted": 0,
				"duplicates": 0,
				"skipped": 0,
				"pages": 0,
				"processed": 0,
				"error": error,
				"sync_log": log_name,
			}

	# Import oldest page first (last fetched), then newer pages.
	# Within each page, connector order is chronological (oldest→newest).
	inserted = 0
	duplicates = 0
	skipped = 0
	upsert_error: str | None = None
	try:
		for batch in reversed(page_batches):
			for row in batch:
				try:
					result = upsert_instagram_message_from_connector(
						account_id=account_id,
						sender_id=sender_id,
						row=row,
						count_unread=count_unread,
						unread_watermark=unread_watermark,
					)
				except Exception as exc:
					partial = True
					upsert_error = str(exc)
					break
				if result is None:
					skipped += 1
					continue
				if result.get("status") == "inserted":
					inserted += 1
				else:
					duplicates += 1
			if upsert_error:
				break
	except Exception as exc:
		partial = True
		upsert_error = str(exc)

	if upsert_error and not error:
		error = upsert_error

	processed = inserted + duplicates
	pages = len(page_batches)
	ok = (not partial) and error is None
	status = "Success" if ok else ("Partial" if processed or pages else "Failed")
	finish_sync_log(
		log_name,
		status=status if status != "Partial" else "Failed",
		processed=processed,
		error=error,
		details={
			"pages": pages,
			"inserted": inserted,
			"duplicates": duplicates,
			"skipped": skipped,
			"partial": partial,
			"count_unread": count_unread,
		},
	)
	return {
		"ok": ok,
		"partial": partial,
		"inserted": inserted,
		"duplicates": duplicates,
		"skipped": skipped,
		"pages": pages,
		"processed": processed,
		"error": error,
		"sync_log": log_name,
		"count_unread": count_unread,
	}


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


def _try_acquire_instagram_inbox_sync_lock() -> str | None:
	"""Atomically acquire Instagram inbox sync lock. Returns ownership token, or None.

	Uses Redis ``SET key token NX EX ttl`` via Frappe's RedisWrapper (``frappe.cache()``).
	Fails **closed** on Redis errors (no concurrent run). Keys are Instagram-only —
	never share Facebook lock keys.
	"""
	try:
		cache = frappe.cache()
		token = secrets.token_hex(16)
		key = cache.make_key(INSTAGRAM_INBOX_SYNC_LOCK_KEY)
		acquired = cache.set(key, token, nx=True, ex=INSTAGRAM_INBOX_SYNC_LOCK_TTL_SEC)
		return token if acquired else None
	except Exception:
		get_logger().info("instagram_inbox_sync_lock_unavailable")
		return None


def _release_instagram_inbox_sync_lock(token: str | None) -> None:
	"""Release lock only if ``token`` still owns it (never deletes another worker's lock)."""
	if not token:
		return
	try:
		cache = frappe.cache()
		key = cache.make_key(INSTAGRAM_INBOX_SYNC_LOCK_KEY)
		cache.eval(_RELEASE_LOCK_IF_OWNER, 1, key, token)
	except Exception:
		pass


def _safe_message_sync_error(raw: Any) -> str:
	"""Short, non-sensitive error text for API responses."""
	text = str(raw or "").strip().split("\n")[0]
	lower = text.lower()
	for needle in ("authorization", "bearer", "api-key", "api_key", "x-api-key", "password", "secret"):
		if needle in lower:
			return "message sync failed"
	return (text[:160] if text else "message sync failed")


def _conversation_name(row: Any) -> str | None:
	if row is None:
		return None
	if isinstance(row, dict):
		return row.get("name")
	return getattr(row, "name", None) or (row[0] if isinstance(row, (list, tuple)) and row else None)


def _get_fair_sync_cursor() -> str:
	try:
		return str(frappe.cache().get_value(INSTAGRAM_FAIR_CURSOR_KEY) or "")
	except Exception:
		return ""


def _set_fair_sync_cursor(name: str) -> None:
	try:
		# No TTL: survive between hourly runs until Redis flush.
		frappe.cache().set_value(INSTAGRAM_FAIR_CURSOR_KEY, name)
	except Exception:
		pass


def select_instagram_conversations_for_message_sync(
	*, max_conversations: int = DEFAULT_MAX_MESSAGE_CONVERSATIONS
) -> list[str]:
	"""Choose Instagram threads for bounded message import (fair, non-starving).

	Quota (of ``max_conversations``):

	- ~50% recently active (``last_message_at`` desc) — includes newly bumped threads.
	- ~25% empty / never-imported (capped; cannot consume the whole batch).
	- remainder fair round-robin by ``name`` using a persisted Instagram-only cache cursor.

	Filters ``provider=Instagram`` only (channel isolation from Facebook). Deduplicates
	selected names. Does **not** treat preview text as proof a message is stored.
	"""
	limit = max(1, min(cint(max_conversations) or DEFAULT_MAX_MESSAGE_CONVERSATIONS, 100))
	recent_quota = max(1, limit // 2)
	empty_quota = max(1, limit // 4)
	fair_quota = max(0, limit - recent_quota - empty_quota)

	selected: list[str] = []
	seen: set[str] = set()

	def _take(names: list[str], quota: int) -> None:
		for name in names:
			if len(selected) >= limit:
				return
			if quota <= 0:
				return
			if name and name not in seen:
				selected.append(name)
				seen.add(name)
				quota -= 1

	recent = frappe.get_all(
		"Omnichannel Conversation",
		filters={"provider": PROVIDER, "is_archived": 0},
		fields=["name"],
		order_by="last_message_at desc, modified desc, name asc",
		limit_page_length=recent_quota,
	)
	_take([_conversation_name(r) for r in (recent or [])], recent_quota)

	empty_rows = frappe.db.sql(
		"""
		select c.name as name
		from `tabOmnichannel Conversation` c
		left join `tabOmnichannel Message` m on m.conversation = c.name
		where c.provider = %s
			and ifnull(c.is_archived, 0) = 0
		group by c.name
		having count(m.name) = 0
		order by c.modified desc, c.name asc
		limit %s
		""",
		(PROVIDER, empty_quota),
		as_dict=True,
	)
	_take([_conversation_name(r) for r in (empty_rows or [])], empty_quota)

	if fair_quota > 0 and len(selected) < limit:
		all_names = [
			_conversation_name(r)
			for r in (
				frappe.get_all(
					"Omnichannel Conversation",
					filters={"provider": PROVIDER, "is_archived": 0},
					fields=["name"],
					order_by="name asc",
					limit_page_length=500,
				)
				or []
			)
		]
		all_names = [n for n in all_names if n]
		if all_names:
			cursor = _get_fair_sync_cursor()
			start = 0
			if cursor in all_names:
				start = (all_names.index(cursor) + 1) % len(all_names)
			fair_picked: list[str] = []
			for i in range(len(all_names)):
				if len(fair_picked) >= fair_quota:
					break
				name = all_names[(start + i) % len(all_names)]
				if name not in seen:
					fair_picked.append(name)
			if fair_picked:
				_take(fair_picked, fair_quota)
				_set_fair_sync_cursor(fair_picked[-1])

	# If quotas left unused (e.g. few conversations), fill from recent remainder.
	if len(selected) < limit:
		more = frappe.get_all(
			"Omnichannel Conversation",
			filters={"provider": PROVIDER, "is_archived": 0},
			fields=["name"],
			order_by="last_message_at desc, modified desc, name asc",
			limit_page_length=limit,
		)
		_take([_conversation_name(r) for r in (more or [])], limit - len(selected))

	return selected[:limit]


def sync_instagram_messages_for_conversations(
	conversation_names: list[str],
	*,
	message_limit: int = 50,
	max_message_pages: int = MAX_MESSAGE_PAGES,
) -> dict[str, Any]:
	"""Run ``sync_instagram_conversation_messages`` per name; isolate errors.

	Deduplication of message rows remains inside
	``upsert_instagram_message_from_connector`` (external id + payload hash).
	"""
	synced = 0
	failed: list[dict[str, str]] = []
	processed_messages = 0
	for name in conversation_names:
		try:
			result = sync_instagram_conversation_messages(
				name,
				limit=message_limit,
				max_pages=max_message_pages,
			)
			if result.get("ok"):
				synced += 1
				processed_messages += cint(result.get("processed") or 0)
			else:
				failed.append(
					{
						"conversation": name,
						"error": _safe_message_sync_error(
							result.get("error") or "message sync failed"
						),
					}
				)
		except Exception as exc:
			failed.append({"conversation": name, "error": _safe_message_sync_error(exc)})
			get_logger().info(
				"instagram_message_sync_conversation_failed conversation=%s", name
			)

	return {
		"ok": len(failed) == 0,
		"selected": len(conversation_names),
		"synced": synced,
		"failed": failed,
		"processed_messages": processed_messages,
	}


def sync_instagram_inbox_bounded(
	*,
	limit: int = 50,
	max_conversation_pages: int = MAX_CONVERSATION_PAGES,
	max_message_conversations: int = DEFAULT_MAX_MESSAGE_CONVERSATIONS,
	message_limit: int = 50,
	max_message_pages: int = MAX_MESSAGE_PAGES,
) -> dict[str, Any]:
	"""Coordinator: Instagram conversation list sync, then bounded per-thread messages.

	Reuses ``sync_instagram_conversations`` and ``sync_instagram_conversation_messages``.
	Overlapping runs are skipped via an Instagram-specific Redis NX lock (never the
	Facebook lock). Caps message fan-out at ``max_message_conversations`` (default 20).
	Does not publish Real-Time, enqueue webhooks, or send outbound messages.
	"""
	token = _try_acquire_instagram_inbox_sync_lock()
	if not token:
		get_logger().info("instagram_inbox_sync_skipped lock_held_or_unavailable")
		return {
			"ok": False,
			"skipped": True,
			"partial": False,
			"processed": 0,
			"error": "Instagram inbox sync already in progress",
		}

	try:
		conversations = sync_instagram_conversations(
			limit=limit, max_pages=max_conversation_pages
		)
		messages: dict[str, Any] = {
			"ok": True,
			"selected": 0,
			"synced": 0,
			"failed": [],
			"processed_messages": 0,
		}

		# Even if the list sync fails, still attempt message sync for known threads.
		selected = select_instagram_conversations_for_message_sync(
			max_conversations=max_message_conversations
		)
		if selected:
			messages = sync_instagram_messages_for_conversations(
				selected,
				message_limit=message_limit,
				max_message_pages=max_message_pages,
			)

		list_ok = bool(conversations.get("ok"))
		msg_failed = bool(messages.get("failed"))
		msg_synced = cint(messages.get("synced") or 0)

		if list_ok and not msg_failed:
			ok = True
			partial = False
			error = None
		elif list_ok and msg_failed:
			ok = False
			partial = True
			n = len(messages.get("failed") or [])
			error = f"Message sync failed for {n} conversation(s)"
		elif (not list_ok) and msg_synced:
			ok = False
			partial = True
			error = _safe_message_sync_error(
				conversations.get("error") or "Instagram conversation list sync failed"
			)
		else:
			ok = False
			partial = False
			error = _safe_message_sync_error(
				conversations.get("error") or "Instagram sync failed"
			)

		return {
			"ok": ok,
			"partial": partial,
			"skipped": False,
			"processed": conversations.get("processed", 0),
			"pages": conversations.get("pages"),
			"sync_log": conversations.get("sync_log"),
			"conversations": conversations,
			"messages": messages,
			"error": error,
		}
	finally:
		_release_instagram_inbox_sync_lock(token)

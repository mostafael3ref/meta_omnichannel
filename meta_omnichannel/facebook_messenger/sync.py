"""Import Facebook conversations/messages from the connector into Frappe."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
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
# Bounded fan-out: after list sync, import messages for at most this many threads.
DEFAULT_MAX_MESSAGE_CONVERSATIONS = 20
FACEBOOK_INBOX_SYNC_LOCK_KEY = "omnichannel:facebook_inbox_sync"
FACEBOOK_INBOX_SYNC_LOCK_TTL_SEC = 900  # 15 minutes; covers worst-case bounded run
FACEBOOK_FAIR_CURSOR_KEY = "omnichannel:facebook_message_sync_fair_cursor"
# Lua: delete lock only when ownership token still matches.
_RELEASE_LOCK_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""

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
	"""Return Omnichannel Channel for ``page_id``, creating or renaming as needed.

	When the channel already exists and ``page_name`` is a non-empty string, update
	``channel_name`` / ``external_account_name`` only if they differ. Blank
	``page_name`` leaves existing names unchanged. Document name (ID), Page ID,
	and linked conversations/messages are never rewritten.
	"""
	existing = frappe.db.get_value(
		"Omnichannel Channel",
		{"provider": PROVIDER, "external_account_id": page_id},
		["name", "channel_name", "external_account_name"],
		as_dict=True,
	)
	if existing:
		cleaned = str(page_name or "").strip()
		if cleaned:
			patch: dict[str, Any] = {}
			if (existing.channel_name or "") != cleaned:
				patch["channel_name"] = cleaned
			if (existing.external_account_name or "") != cleaned:
				patch["external_account_name"] = cleaned
			if patch:
				frappe.db.set_value(
					"Omnichannel Channel",
					existing.name,
					patch,
					update_modified=False,
				)
		return existing.name
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


def _as_compare_datetime(value: Any) -> datetime | None:
	"""Normalize a DB/connector datetime for ordering comparisons (naive)."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.replace(tzinfo=None) if value.tzinfo is not None else value
	return parse_connector_datetime_for_db(value)


def _merge_conversation_preview_fields(
	*,
	existing_at: Any,
	existing_preview: Any,
	candidate_at: datetime | None,
	candidate_preview: str,
) -> dict[str, Any]:
	"""Return preview fields to write, or {} if candidate must not overwrite.

	Rules:
	- Never move ``last_message_at`` backwards.
	- Never apply preview/time without a valid candidate timestamp.
	- On equal timestamps, never replace a non-empty existing preview with a
	  different candidate (avoids unrelated/older same-second text). Only fill
	  when the existing preview is blank.
	- Older candidates are ignored entirely (protects outbound / newer connector state).
	"""
	preview = (candidate_preview or "").strip()
	if candidate_at is None:
		return {}

	cur_at = _as_compare_datetime(existing_at)
	existing_text = (existing_preview or "").strip()
	if cur_at is None or candidate_at > cur_at:
		out: dict[str, Any] = {"last_message_at": candidate_at}
		if preview:
			out["last_message_preview"] = preview[:500]
		elif cur_at is None and not existing_text:
			# Brand-new row may store empty preview with a valid timestamp.
			out["last_message_preview"] = (candidate_preview or "")[:500]
		return out

	if candidate_at == cur_at and preview and not existing_text:
		return {"last_message_preview": preview[:500]}

	return {}


def upsert_conversation_from_connector(row: dict[str, Any]) -> str | None:
	page_id = str(row.get("page_id") or row.get("pageId") or "").strip()
	sender_id = str(
		row.get("sender_id") or row.get("senderId") or row.get("participant_id") or row.get("psid") or ""
	).strip()
	if not page_id or not sender_id:
		return None

	channel = _ensure_facebook_channel(page_id, row.get("page_name") or row.get("pageName"))
	key = thread_key(page_id, sender_id)
	existing = frappe.db.get_value(
		"Omnichannel Conversation",
		{"external_thread_key": key},
		["name", "last_message_at", "last_message_preview"],
		as_dict=True,
	)
	preview = str(
		row.get("last_message")
		or row.get("last_message_text")
		or row.get("snippet")
		or row.get("preview")
		or ""
	)[:500]
	last_at_raw = row.get("last_message_at") or row.get("updated_at") or row.get("timestamp")
	participant_name = (
		row.get("participant_name")
		or row.get("sender_name")
		or row.get("name")
		or sender_id
	)
	parsed_last_at = parse_connector_datetime_for_db(last_at_raw)

	values = {
		"channel": channel,
		"provider": PROVIDER,
		"external_thread_key": key,
		"page_id": page_id,
		"participant_id": sender_id,
		"participant_name": participant_name,
	}

	if existing:
		values.update(
			_merge_conversation_preview_fields(
				existing_at=existing.last_message_at,
				existing_preview=existing.last_message_preview,
				candidate_at=parsed_last_at,
				candidate_preview=preview,
			)
		)
		frappe.db.set_value("Omnichannel Conversation", existing.name, values, update_modified=True)
		return existing.name

	# Insert: only attach preview/time when timestamp parsed (no fabricated freshness).
	if parsed_last_at is not None:
		values["last_message_at"] = parsed_last_at
		values["last_message_preview"] = preview
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


def _enqueue_facebook_attachment_persist_safe(message_name: str) -> None:
	"""Call ``enqueue_facebook_attachment_persist``; never raise into sync."""
	try:
		from meta_omnichannel.jobs.sync_jobs import enqueue_facebook_attachment_persist

		# Primary attachment only (index 0) — matches stored ``attachment_url``.
		enqueue_facebook_attachment_persist(message_name, 0)
	except Exception:
		get_logger().info(
			"facebook_attachment_persist_enqueue_failed message=%s",
			message_name,
		)


def schedule_facebook_attachment_persist_after_commit(message_name: str) -> None:
	"""Register attachment persist enqueue after a successful DB commit.

	Frappe v15 supports ``frappe.db.after_commit.add(callback)`` (same hook used
	internally by ``frappe.enqueue(..., enqueue_after_commit=True)``). Callbacks
	run only after ``COMMIT``; they are discarded on rollback — so jobs are never
	queued for uncommitted message rows.
	"""
	name = (message_name or "").strip()
	if not name:
		return
	try:
		frappe.db.after_commit.add(
			lambda message_name=name: _enqueue_facebook_attachment_persist_safe(message_name)
		)
	except Exception:
		get_logger().info(
			"facebook_attachment_persist_schedule_failed message=%s",
			name,
		)


def _maybe_schedule_attachment_persist(
	message_name: str, *, known_attachment_url: str | None = None
) -> None:
	"""Schedule persist when the message has primary attachment metadata.

	Text-only messages (no ``attachment_url``) are never scheduled. Does not invent
	URLs or multi-attachment indexes beyond the single stored primary field.
	"""
	name = (message_name or "").strip()
	if not name:
		return
	if known_attachment_url is not None:
		if not str(known_attachment_url).strip():
			return
	else:
		try:
			stored = frappe.db.get_value("Omnichannel Message", name, "attachment_url")
		except Exception:
			get_logger().info(
				"facebook_attachment_persist_lookup_failed message=%s",
				name,
			)
			return
		if not (stored or "").strip():
			return
	schedule_facebook_attachment_persist_after_commit(name)


def _apply_inbound_conversation_metadata(
	conversation: str,
	*,
	raw_timestamp: Any,
	count_unread: bool,
	unread_watermark: datetime | None,
) -> None:
	"""Update ``last_inbound_at`` / unread after a newly inserted inbound message.

	Uses valid connector timestamps only — never the missing→``now`` fallback used
	for message ``sent_at``. Unread increments require a pre-sync watermark and a
	strictly newer connector time (see ``sync_conversation_messages``).

	Limitation: the first sync of an empty thread (initial import) never
	increments unread, even when the same job also pulls a live tip plus older
	history pages. Live tips on already-synced threads still increment when newer
	than the pre-sync watermark. Historical backfill pages are not counted.
	"""
	if not connector_timestamp_is_present(raw_timestamp):
		return
	parsed = parse_connector_datetime_for_db(raw_timestamp)
	if parsed is None:
		return
	_advance_last_inbound_at(conversation, parsed)
	if count_unread and unread_watermark is not None and parsed > unread_watermark:
		_increment_unread_count(conversation)


def upsert_message_from_connector(
	conversation: str,
	channel: str,
	row: dict[str, Any],
	*,
	count_unread: bool = False,
	unread_watermark: datetime | None = None,
) -> str | None:
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
			# Re-sync of an existing attachment message: enqueue if not yet Ready.
			_maybe_schedule_attachment_persist(existing.name)
			return existing.name
		# Unique external_message_id elsewhere: never insert a duplicate.
		# Do not schedule attachment persist — the row belongs to another thread.
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
		_maybe_schedule_attachment_persist(existing_fp)
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
	if direction == "Inbound":
		_apply_inbound_conversation_metadata(
			conversation,
			raw_timestamp=raw_timestamp,
			count_unread=count_unread,
			unread_watermark=unread_watermark,
		)
	# New insert: schedule only when primary attachment metadata was stored.
	_maybe_schedule_attachment_persist(doc.name, known_attachment_url=attachment_url)
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
	"""Import message batches for one conversation. offset 0 = newest batch per contract.

	Unread / ``last_inbound_at`` policy (applied inside ``upsert_message_from_connector``):
	- Snapshot a pre-sync watermark = ``last_inbound_at or last_message_at``.
	- Initial import (no local messages yet): advance ``last_inbound_at`` from valid
	  inbound connector times, but never increment ``unread_count``.
	- Later syncs: increment unread only for new inbound inserts whose connector
	  timestamp is strictly after the pre-sync watermark (historical backfill skipped).
	- Dedup hits return early and never bump metadata.

	Limitation: a first sync of an empty thread that also pulls a live tip will not
	count that tip as unread; tips on already-synced threads will.
	"""
	conv = frappe.get_doc("Omnichannel Conversation", conversation_name)
	if conv.provider != PROVIDER:
		frappe.throw("Not a Facebook conversation")

	# Pre-sync watermark + initial-import guard (see docstring limitation).
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
				if upsert_message_from_connector(
					conv.name,
					conv.channel,
					row,
					count_unread=count_unread,
					unread_watermark=unread_watermark,
				):
					processed += 1
			pages += 1
			has_more = bool(payload.get("has_more"))
			next_offset = payload.get("next_offset")
			if not has_more:
				break
			offset = cint(next_offset if next_offset is not None else offset + limit)

		# Refresh preview only when local latest is at least as fresh as conversation.
		# Failures never reach here — ConnectorError skips preview updates entirely.
		_maybe_refresh_conversation_preview_from_local_messages(conversation_name)
		frappe.db.set_value("Omnichannel Channel", conv.channel, "last_synced_at", now_datetime())
		finish_sync_log(log_name, status="Success", processed=processed, details={"pages": pages})
		return {"ok": True, "processed": processed, "pages": pages, "sync_log": log_name}
	except ConnectorError as exc:
		finish_sync_log(log_name, status="Failed", processed=processed, error=str(exc))
		return {"ok": False, "processed": processed, "error": str(exc), "sync_log": log_name}


def _maybe_refresh_conversation_preview_from_local_messages(conversation_name: str) -> None:
	"""Update conversation preview from local messages without rolling time backwards."""
	current = frappe.db.get_value(
		"Omnichannel Conversation",
		conversation_name,
		["last_message_at", "last_message_preview"],
		as_dict=True,
	)
	if not current:
		return
	# Ignore NULL sent_at rows — they must not fabricate freshness.
	latest = frappe.db.sql(
		"""
		select text_content, sent_at, name
		from `tabOmnichannel Message`
		where conversation=%s
			and ifnull(is_internal_note,0)=0
			and sent_at is not null
		order by sent_at desc, name desc
		limit 1
		""",
		conversation_name,
		as_dict=True,
	)
	if not latest:
		return
	row = latest[0]
	candidate_at = _as_compare_datetime(row.sent_at)
	patch = _merge_conversation_preview_fields(
		existing_at=current.last_message_at,
		existing_preview=current.last_message_preview,
		candidate_at=candidate_at,
		candidate_preview=str(row.text_content or ""),
	)
	if patch:
		frappe.db.set_value("Omnichannel Conversation", conversation_name, patch, update_modified=False)


def _try_acquire_facebook_inbox_sync_lock() -> str | None:
	"""Atomically acquire inbox sync lock. Returns ownership token, or None.

	Uses Redis ``SET key token NX EX ttl`` via Frappe's RedisWrapper (``frappe.cache()``).
	Fails **closed** on Redis errors (no concurrent run).
	"""
	try:
		cache = frappe.cache()
		token = secrets.token_hex(16)
		key = cache.make_key(FACEBOOK_INBOX_SYNC_LOCK_KEY)
		acquired = cache.set(key, token, nx=True, ex=FACEBOOK_INBOX_SYNC_LOCK_TTL_SEC)
		return token if acquired else None
	except Exception:
		get_logger().info("facebook_inbox_sync_lock_unavailable")
		return None


def _release_facebook_inbox_sync_lock(token: str | None) -> None:
	"""Release lock only if ``token`` still owns it (never deletes another worker's lock)."""
	if not token:
		return
	try:
		cache = frappe.cache()
		key = cache.make_key(FACEBOOK_INBOX_SYNC_LOCK_KEY)
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
		return str(frappe.cache().get_value(FACEBOOK_FAIR_CURSOR_KEY) or "")
	except Exception:
		return ""


def _set_fair_sync_cursor(name: str) -> None:
	try:
		# No TTL: survive between hourly runs until Redis flush.
		frappe.cache().set_value(FACEBOOK_FAIR_CURSOR_KEY, name)
	except Exception:
		pass


def select_facebook_conversations_for_message_sync(
	*, max_conversations: int = DEFAULT_MAX_MESSAGE_CONVERSATIONS
) -> list[str]:
	"""Choose Facebook threads for bounded message import (fair, non-starving).

	Quota (of ``max_conversations``):

	- ~50% recently active (``last_message_at`` desc) — includes newly bumped threads.
	- ~25% empty / never-imported (capped; cannot consume the whole batch).
	- remainder fair round-robin by ``name`` using a persisted cache cursor.

	Does **not** treat preview text as proof a message is stored.
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


def sync_facebook_messages_for_conversations(
	conversation_names: list[str],
	*,
	message_limit: int = 50,
	max_message_pages: int = MAX_MESSAGE_PAGES,
) -> dict[str, Any]:
	"""Run ``sync_conversation_messages`` for each name; isolate per-conversation errors."""
	synced = 0
	failed: list[dict[str, str]] = []
	processed_messages = 0
	for name in conversation_names:
		try:
			result = sync_conversation_messages(
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
						"error": _safe_message_sync_error(result.get("error") or "message sync failed"),
					}
				)
		except Exception as exc:
			failed.append({"conversation": name, "error": _safe_message_sync_error(exc)})
			get_logger().info(
				"facebook_message_sync_conversation_failed conversation=%s", name
			)

	return {
		"ok": len(failed) == 0,
		"selected": len(conversation_names),
		"synced": synced,
		"failed": failed,
		"processed_messages": processed_messages,
	}


def sync_facebook_inbox_bounded(
	*,
	limit: int = 50,
	max_conversation_pages: int = MAX_CONVERSATION_PAGES,
	max_message_conversations: int = DEFAULT_MAX_MESSAGE_CONVERSATIONS,
	message_limit: int = 50,
	max_message_pages: int = MAX_MESSAGE_PAGES,
) -> dict[str, Any]:
	"""Coordinator: conversation list sync, then bounded per-thread message sync.

	Reuses ``sync_facebook_conversations`` and ``sync_conversation_messages``.
	Overlapping runs are skipped via atomic Redis NX lock.
	"""
	token = _try_acquire_facebook_inbox_sync_lock()
	if not token:
		get_logger().info("facebook_inbox_sync_skipped lock_held_or_unavailable")
		return {
			"ok": False,
			"skipped": True,
			"partial": False,
			"processed": 0,
			"error": "Facebook inbox sync already in progress",
		}

	try:
		conversations = sync_facebook_conversations(
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
		selected = select_facebook_conversations_for_message_sync(
			max_conversations=max_message_conversations
		)
		if selected:
			messages = sync_facebook_messages_for_conversations(
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
				conversations.get("error") or "Facebook conversation list sync failed"
			)
		else:
			ok = False
			partial = False
			error = _safe_message_sync_error(
				conversations.get("error") or "Facebook sync failed"
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
		_release_facebook_inbox_sync_lock(token)

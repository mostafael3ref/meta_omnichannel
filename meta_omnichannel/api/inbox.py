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


def _page_name_from_channel_fields(
	external_account_name: Any, channel_name: Any
) -> str | None:
	"""Prefer Channel.external_account_name, then channel_name."""
	for raw in (external_account_name, channel_name):
		label = str(raw or "").strip()
		if label:
			return label
	return None


def _resolve_page_name_for_channel(channel_name: str | None) -> str | None:
	"""Single-channel lookup (get_conversation / list_messages)."""
	name = str(channel_name or "").strip()
	if not name:
		return None
	row = frappe.db.get_value(
		"Omnichannel Channel",
		name,
		["external_account_name", "channel_name"],
		as_dict=True,
	)
	if not row:
		return None
	return _page_name_from_channel_fields(row.external_account_name, row.channel_name)


def _channel_page_names(channel_names: list[Any]) -> dict[str, str]:
	"""Batch map Omnichannel Channel name → display page_name (no N+1)."""
	names = sorted({str(n or "").strip() for n in channel_names if str(n or "").strip()})
	if not names:
		return {}
	rows = frappe.get_all(
		"Omnichannel Channel",
		filters={"name": ["in", names]},
		fields=["name", "external_account_name", "channel_name"],
		limit_page_length=len(names),
	)
	out: dict[str, str] = {}
	for row in rows:
		label = _page_name_from_channel_fields(row.external_account_name, row.channel_name)
		if label:
			out[row.name] = label
	return out


def _serialize_conversation(row, *, page_name: str | None = None) -> dict:
	"""Serialize a conversation row. Pass ``page_name`` from a batched Channel lookup."""
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
		"page_name": (str(page_name).strip() if page_name else None) or None,
	}


_MESSAGE_LIST_FIELDS = (
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
)


def _message_row_name(row: Any) -> str:
	if isinstance(row, dict):
		return str(row.get("name") or "")
	return str(getattr(row, "name", None) or "")


def _message_row_dict(row: Any) -> dict[str, Any]:
	"""Normalize get_all rows (dict / _dict / SimpleNamespace) for JSON-safe enrichment."""
	if isinstance(row, dict):
		return {k: row.get(k) for k in _MESSAGE_LIST_FIELDS}
	as_dict = getattr(row, "as_dict", None)
	if callable(as_dict):
		data = as_dict()
		if isinstance(data, dict):
			return {k: data.get(k) for k in _MESSAGE_LIST_FIELDS}
	return {k: getattr(row, k, None) for k in _MESSAGE_LIST_FIELDS}


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
	page_names = _channel_page_names([r.channel for r in rows])
	return {
		"items": [
			_serialize_conversation(r, page_name=page_names.get(r.channel)) for r in rows
		],
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
	return _serialize_conversation(
		doc, page_name=_resolve_page_name_for_channel(doc.channel)
	)


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

	# Enrich with Ready private File URLs (conversation auth already checked above).
	from meta_omnichannel.facebook_messenger import media as fb_media

	local_urls = fb_media.map_ready_attachment_file_urls(
		[_message_row_name(r) for r in rows], attachment_index=0
	)
	items: list[dict[str, Any]] = []
	for row in rows:
		item = _message_row_dict(row)
		# Trusted /private/files/... only when Ready+valid; never invent paths.
		item["attachment_file_url"] = local_urls.get(item.get("name"))
		items.append(item)

	return {
		"items": items,
		"total": total,
		"limit": limit,
		"offset": offset,
		"has_more": offset + len(rows) < total,
		"next_offset": offset + len(rows) if offset + len(rows) < total else None,
		"conversation": _serialize_conversation(
			conv, page_name=_resolve_page_name_for_channel(conv.channel)
		),
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


def _read_upload_bounded(
	upload, *, max_bytes: int, label: str
) -> tuple[bytes, str | None, str | None]:
	"""Read at most ``max_bytes + 1`` from the upload stream (reject oversized)."""
	filename = getattr(upload, "filename", None) or getattr(upload, "name", None)
	content_type = getattr(upload, "content_type", None) or getattr(upload, "mimetype", None)
	stream = getattr(upload, "stream", None)
	if stream is not None and hasattr(stream, "read"):
		data = stream.read(max_bytes + 1)
	elif hasattr(upload, "read"):
		data = upload.read(max_bytes + 1)
	else:
		frappe.throw(_("{0} upload is required").format(label), frappe.ValidationError)
	if data is None:
		data = b""
	if not isinstance(data, (bytes, bytearray)):
		data = bytes(data)
	else:
		data = bytes(data)
	if len(data) > max_bytes:
		frappe.throw(_("{0} exceeds 8 MiB limit").format(label), frappe.ValidationError)
	if not data:
		frappe.throw(_("{0} upload is required").format(label), frappe.ValidationError)
	return data, (str(filename).strip() if filename else None), (
		str(content_type).strip() if content_type else None
	)


def _read_upload_image_bounded(upload, *, max_bytes: int) -> tuple[bytes, str | None, str | None]:
	"""Backward-compatible image upload reader (8 MiB bound)."""
	return _read_upload_bounded(upload, max_bytes=max_bytes, label="Image")


def _multipart_upload(field_name: str):
	"""Return the multipart upload for ``field_name``, or None if missing."""
	files = getattr(getattr(frappe, "request", None), "files", None) or {}
	if hasattr(files, "get"):
		return files.get(field_name)
	if isinstance(files, dict):
		return files.get(field_name)
	return None


@frappe.whitelist()
def send_image_message(
	conversation: str, client_nonce: str, filename: str | None = None
) -> dict:
	"""Send one Facebook Messenger image from multipart field ``image``."""
	ensure_omnichannel_agent()
	client_nonce = (client_nonce or "").strip()
	if not client_nonce:
		frappe.throw(_("client_nonce is required"), frappe.ValidationError)

	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if conv.provider != "Facebook":
		frappe.throw(
			_("Outbound messaging for {0} is not available until the connector API is confirmed").format(
				conv.provider
			)
		)

	upload = _multipart_upload("image")
	if upload is None:
		frappe.throw(_("Image upload is required"), frappe.ValidationError)

	from meta_omnichannel.facebook_messenger.adapter import MAX_OUTBOUND_IMAGE_BYTES
	from meta_omnichannel.facebook_messenger.outbound import send_facebook_image

	image_bytes, upload_name, upload_mime = _read_upload_bounded(
		upload, max_bytes=MAX_OUTBOUND_IMAGE_BYTES, label="Image"
	)
	safe_filename = (filename or "").strip() or upload_name

	return send_facebook_image(
		conversation_name=conversation,
		client_nonce=client_nonce,
		image_bytes=image_bytes,
		filename=safe_filename,
		content_type=upload_mime,
	)


@frappe.whitelist()
def send_document_message(
	conversation: str, client_nonce: str, filename: str | None = None
) -> dict:
	"""Send one Facebook Messenger PDF/DOCX from multipart field ``document``."""
	ensure_omnichannel_agent()
	client_nonce = (client_nonce or "").strip()
	if not client_nonce:
		frappe.throw(_("client_nonce is required"), frappe.ValidationError)

	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if conv.provider != "Facebook":
		frappe.throw(
			_("Outbound messaging for {0} is not available until the connector API is confirmed").format(
				conv.provider
			)
		)

	upload = _multipart_upload("document")
	if upload is None:
		frappe.throw(_("Document upload is required"), frappe.ValidationError)

	from meta_omnichannel.facebook_messenger.adapter import MAX_OUTBOUND_DOCUMENT_BYTES
	from meta_omnichannel.facebook_messenger.outbound import send_facebook_document

	document_bytes, upload_name, upload_mime = _read_upload_bounded(
		upload, max_bytes=MAX_OUTBOUND_DOCUMENT_BYTES, label="Document"
	)
	safe_filename = (filename or "").strip() or upload_name

	return send_facebook_document(
		conversation_name=conversation,
		client_nonce=client_nonce,
		document_bytes=document_bytes,
		filename=safe_filename,
		content_type=upload_mime,
	)


# --- ERPNext File Library (selectable outbound attachments) -----------------
# List returns safe metadata only (never file_url / bytes). Send re-checks File
# + attached-document read access, reads a bounded byte window from disk, then
# reuses send_facebook_image / send_facebook_document (which copy into a new
# private outbound File and leave the source File untouched).

_LIBRARY_MAX_BYTES = 8 * 1024 * 1024
_LIBRARY_TYPE_BY_EXT: dict[str, tuple[str, str]] = {
	".jpg": ("image", "image/jpeg"),
	".jpeg": ("image", "image/jpeg"),
	".png": ("image", "image/png"),
	".gif": ("image", "image/gif"),
	".pdf": ("document", "application/pdf"),
	".docx": (
		"document",
		"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	),
}
_LIBRARY_EXTENSIONS = tuple(_LIBRARY_TYPE_BY_EXT.keys())


def _library_extension(file_name: str | None) -> str:
	name = str(file_name or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
	if "." not in name:
		return ""
	return "." + name.rsplit(".", 1)[-1].lower()


def _library_type_for_filename(file_name: str | None) -> tuple[str, str] | None:
	return _LIBRARY_TYPE_BY_EXT.get(_library_extension(file_name))


def _sanitize_library_search(search: str | None) -> str:
	"""Strip LIKE wildcards so callers cannot broaden the extension filter."""
	return (
		str(search or "")
		.strip()
		.replace("%", "")
		.replace("_", "")
		.replace("\\", "")
	)


def _library_list_filters(search: str | None) -> list[list]:
	filters: list[list] = [
		["is_folder", "=", 0],
		["file_size", ">", 0],
		["file_size", "<=", _LIBRARY_MAX_BYTES],
	]
	term = _sanitize_library_search(search)
	if term:
		filters.append(["file_name", "like", f"%{term}%"])
	return filters


def _library_extension_or_filters() -> list[list]:
	# Case-insensitive LIKE on typical MySQL/MariaDB collations; include both cases
	# for sites that use binary/CS collations.
	out: list[list] = []
	for ext in _LIBRARY_EXTENSIONS:
		out.append(["file_name", "like", f"%{ext}"])
		upper = ext.upper()
		if upper != ext:
			out.append(["file_name", "like", f"%{upper}"])
	return out


def _file_doc_has_read(file_doc) -> bool:
	"""True when the session user may read this File row (Frappe File ACL)."""
	try:
		has_perm = getattr(file_doc, "has_permission", None)
		if callable(has_perm):
			return bool(has_perm("read"))
		return bool(frappe.has_permission("File", "read", doc=file_doc))
	except Exception:
		return False


def _attached_document_has_read(file_doc) -> bool:
	"""Re-check read access on the underlying attached document when present."""
	doctype = str(getattr(file_doc, "attached_to_doctype", None) or "").strip()
	name = str(getattr(file_doc, "attached_to_name", None) or "").strip()
	if not doctype or not name:
		return True
	try:
		if not frappe.has_permission(doctype, "read", doc=name):
			return False
		ref = frappe.get_doc(doctype, name)
		has_perm = getattr(ref, "has_permission", None)
		if callable(has_perm) and not has_perm("read"):
			return False
		return True
	except Exception:
		return False


def _user_can_select_library_file(file_doc) -> bool:
	if cint(getattr(file_doc, "is_folder", 0)):
		return False
	if not _library_type_for_filename(getattr(file_doc, "file_name", None)):
		return False
	size = cint(getattr(file_doc, "file_size", 0))
	if size <= 0 or size > _LIBRARY_MAX_BYTES:
		return False
	if not _file_doc_has_read(file_doc):
		return False
	if not _attached_document_has_read(file_doc):
		return False
	return True


def _serialize_library_file(file_doc) -> dict[str, Any]:
	"""Safe metadata only — never ``file_url`` or content bytes."""
	kind_mime = _library_type_for_filename(getattr(file_doc, "file_name", None))
	kind, content_type = kind_mime if kind_mime else ("", "")
	return {
		"name": str(getattr(file_doc, "name", "") or ""),
		"file_name": str(getattr(file_doc, "file_name", None) or ""),
		"file_size": cint(getattr(file_doc, "file_size", 0)),
		"kind": kind,
		"content_type": content_type,
		"is_private": bool(cint(getattr(file_doc, "is_private", 0))),
	}


def _open_library_file_for_send(file: str, *, expected_kind: str):
	"""Resolve File by name, re-check ACL, return ``(file_doc, kind, mime_hint)``."""
	name = str(file or "").strip()
	if not name:
		frappe.throw(_("File is required"), frappe.ValidationError)
	if expected_kind not in ("image", "document"):
		frappe.throw(_("Invalid attachment kind"), frappe.ValidationError)

	file_doc = frappe.get_doc("File", name)
	if not _user_can_select_library_file(file_doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	kind_mime = _library_type_for_filename(getattr(file_doc, "file_name", None))
	if not kind_mime or kind_mime[0] != expected_kind:
		label = "image" if expected_kind == "image" else "document"
		frappe.throw(
			_("Selected file is not a valid {0}").format(label),
			frappe.ValidationError,
		)
	return file_doc, kind_mime[0], kind_mime[1]


def _read_library_file_bounded(file_doc, *, max_bytes: int, label: str) -> bytes:
	"""Read at most ``max_bytes + 1`` from the File on disk (never unbounded)."""
	# Recheck ACL immediately before any byte I/O.
	if not _user_can_select_library_file(file_doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	data: bytes | None = None
	get_full_path = getattr(file_doc, "get_full_path", None)
	if callable(get_full_path):
		path = get_full_path()
		if not path:
			frappe.throw(_("{0} content is unavailable").format(label), frappe.ValidationError)
		# validate_file_path (inside get_full_path) must keep us under the site files root.
		with open(path, "rb") as handle:
			chunk = handle.read(max_bytes + 1)
		data = b"" if chunk is None else bytes(chunk)
	else:
		# Fallback for test doubles / in-memory File rows without a path helper.
		raw = getattr(file_doc, "content", None)
		if isinstance(raw, (bytes, bytearray)):
			data = bytes(raw[: max_bytes + 1])
		elif isinstance(raw, str):
			encoded = raw.encode("utf-8", errors="ignore")
			data = encoded[: max_bytes + 1]
		else:
			frappe.throw(_("{0} content is unavailable").format(label), frappe.ValidationError)

	if len(data) > max_bytes:
		frappe.throw(_("{0} exceeds 8 MiB limit").format(label), frappe.ValidationError)
	if not data:
		frappe.throw(_("{0} content is unavailable").format(label), frappe.ValidationError)
	return data


def _send_from_library_file(
	*,
	conversation: str,
	file: str,
	client_nonce: str,
	expected_kind: str,
) -> dict:
	ensure_omnichannel_agent()
	client_nonce = (client_nonce or "").strip()
	if not client_nonce:
		frappe.throw(_("client_nonce is required"), frappe.ValidationError)

	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if conv.provider != "Facebook":
		frappe.throw(
			_("Outbound messaging for {0} is not available until the connector API is confirmed").format(
				conv.provider
			)
		)

	from meta_omnichannel.facebook_messenger.adapter import (
		MAX_OUTBOUND_DOCUMENT_BYTES,
		MAX_OUTBOUND_IMAGE_BYTES,
	)
	from meta_omnichannel.facebook_messenger.outbound import (
		send_facebook_document,
		send_facebook_image,
	)

	file_doc, _kind, mime_hint = _open_library_file_for_send(
		file, expected_kind=expected_kind
	)
	max_bytes = (
		MAX_OUTBOUND_IMAGE_BYTES if expected_kind == "image" else MAX_OUTBOUND_DOCUMENT_BYTES
	)
	label = "Image" if expected_kind == "image" else "Document"
	payload = _read_library_file_bounded(file_doc, max_bytes=max_bytes, label=label)
	safe_filename = str(getattr(file_doc, "file_name", None) or "").strip() or None

	# Outbound helpers validate magic bytes, persist a *new* private File on the
	# Omnichannel Message, and leave the source ERPNext File unchanged.
	if expected_kind == "image":
		return send_facebook_image(
			conversation_name=conversation,
			client_nonce=client_nonce,
			image_bytes=payload,
			filename=safe_filename,
			content_type=mime_hint,
		)
	return send_facebook_document(
		conversation_name=conversation,
		client_nonce=client_nonce,
		document_bytes=payload,
		filename=safe_filename,
		content_type=mime_hint,
	)


@frappe.whitelist()
def list_selectable_outbound_files(
	search: str | None = None,
	limit: int = 30,
	offset: int = 0,
) -> dict:
	"""List ERPNext Files the agent may pick for Messenger outbound send.

	Returns safe metadata only (no ``file_url``, no bytes). Candidates are limited
	to JPEG/PNG/GIF/PDF/DOCX at most 8 MiB. Each row is re-checked for File read
	permission and underlying attached-document read access.
	"""
	ensure_omnichannel_agent()
	limit = max(1, min(cint(limit), 100))
	offset = max(0, cint(offset))

	filters = _library_list_filters(search)
	or_filters = _library_extension_or_filters()
	rows = frappe.get_list(
		"File",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"file_name",
			"file_size",
			"is_private",
			"is_folder",
			"attached_to_doctype",
			"attached_to_name",
		],
		order_by="modified desc",
		limit_page_length=limit,
		limit_start=offset,
	)

	items: list[dict[str, Any]] = []
	for row in rows:
		name = str(
			getattr(row, "name", None)
			or (row.get("name") if isinstance(row, dict) else "")
			or ""
		)
		if not name:
			continue
		try:
			file_doc = frappe.get_doc("File", name)
		except Exception:
			continue
		if not _user_can_select_library_file(file_doc):
			continue
		items.append(_serialize_library_file(file_doc))

	# Pagination follows the DB candidate window (extension/size/search + File
	# permission_query_conditions). Per-row attached-doc ACL may still drop items,
	# so ``items`` can be shorter than ``limit`` even when ``has_more`` is true.
	has_more = len(rows) == limit
	return {
		"items": items,
		"total": offset + len(rows) + (1 if has_more else 0),
		"limit": limit,
		"offset": offset,
		"has_more": has_more,
		"next_offset": offset + len(rows) if has_more else None,
	}


@frappe.whitelist()
def send_image_from_file(conversation: str, file: str, client_nonce: str) -> dict:
	"""Send one Facebook Messenger image from an existing ERPNext File (by name)."""
	return _send_from_library_file(
		conversation=conversation,
		file=file,
		client_nonce=client_nonce,
		expected_kind="image",
	)


@frappe.whitelist()
def send_document_from_file(conversation: str, file: str, client_nonce: str) -> dict:
	"""Send one Facebook Messenger PDF/DOCX from an existing ERPNext File (by name)."""
	return _send_from_library_file(
		conversation=conversation,
		file=file,
		client_nonce=client_nonce,
		expected_kind="document",
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

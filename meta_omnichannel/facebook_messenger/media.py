"""Persist Facebook Messenger attachments as private Frappe Files.

Trusted system operation (background/worker context). Conversation-scoped
agent access and a permissioned serve API are separate steps — this module
does not grant new permissions or return ``file_url`` to callers.

Concurrency (Redis)
-------------------
Uses Frappe's Redis cache client (``frappe.cache()`` → ``RedisWrapper``) with
atomic ``SET key token NX EX ttl`` and tokenized Lua release/renew — same
pattern as Facebook inbox sync / inbound replay protection.

Lock key: ``omnichannel:fb_attach:{message_name}:{attachment_index}``
TTL: ``ATTACHMENT_LOCK_TTL_SEC`` (aligned with RQ ``long`` job timeout).
Contention: non-blocking; returns a retryable result (never marks Failed).

RQ job ownership
----------------
``jobs.sync_jobs.run_facebook_attachment_persist`` must:

1. Acquire ``attachment_persist_lock``
2. Call ``persist_message_attachment_unlocked`` (not the public wrapper)
3. On ``lock_lost`` / ownership failure: ``rollback()`` (never commit)
4. Otherwise re-check ``still_owned()`` then ``commit()`` **before** unlock

Limitation: the ownership check immediately before ``commit()`` is not an
atomic Redis+MariaDB operation.

This module never uses ``get_doc(..., for_update=True)`` across Connector I/O
and never calls ``frappe.db.commit()`` / ``rollback()``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.file_manager import get_file_path, save_file

from meta_omnichannel.facebook_messenger.adapter import (
	AttachmentPayload,
	FacebookConnectorAdapter,
	MAX_OUTBOUND_DOCUMENT_BYTES,
)
from meta_omnichannel.meta_connectors.errors import ConnectorError

_OUTBOUND_DOCUMENT_MIME_TYPES = frozenset(
	{
		"application/pdf",
		"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	}
)

PROVIDER = "Facebook"
MESSAGE_DOCTYPE = "Omnichannel Message"
CHILD_DOCTYPE = "Omnichannel Message Attachment"
CHILD_PARENTFIELD = "attachments"
SYSTEM_OWNER = "Administrator"
ATTACHMENT_LOCK_PREFIX = "omnichannel:fb_attach"
# Align with Frappe default ``long`` queue timeout (1500s) but bound below it
# so lock expires if the worker dies. ConnectorClient uses connect=5s + read=15s
# (see meta_connectors.client); renew before/after that HTTP call keeps ownership
# across the bounded download. Local hash/save/DB work is not HTTP-bound.
ATTACHMENT_LOCK_TTL_SEC = 900
# Documented upper bound for Connector HTTP only (not disk/DB).
CONNECTOR_HTTP_BUDGET_SEC = 5.0 + 15.0
_SAFE_STEM = re.compile(r"[^A-Za-z0-9_-]+")
# Lua: delete lock only when ownership token still matches.
_RELEASE_LOCK_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""
# Lua: refresh TTL only when ownership token still matches.
_RENEW_LOCK_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


class AttachmentPersistLockLost(Exception):
	"""Raised when this worker no longer owns the Redis attachment lock."""


def _parse_attachment_index(attachment_index: Any) -> int:
	if isinstance(attachment_index, bool) or not isinstance(attachment_index, int):
		frappe.throw(
			_("attachment_index must be a non-negative integer"),
			frappe.ValidationError,
		)
	if attachment_index < 0:
		frappe.throw(
			_("attachment_index must be a non-negative integer"),
			frappe.ValidationError,
		)
	return attachment_index


def attachment_persist_lock_key(message_name: str, attachment_index: int) -> str:
	"""Logical Redis lock key for one (message, attachment_index) pair."""
	return f"{ATTACHMENT_LOCK_PREFIX}:{message_name}:{int(attachment_index)}"


def attachment_persist_job_id(message_name: str, attachment_index: int) -> str:
	"""Deterministic RQ ``job_id`` for one attachment (before site prefix)."""
	return f"omnichannel_fb_attach:{message_name}:{int(attachment_index)}"


def _decode_redis_value(value: Any) -> str | None:
	if value is None:
		return None
	if isinstance(value, bytes):
		return value.decode()
	return str(value)


@dataclass
class AttachmentPersistLock:
	"""Owned Redis lock for one (message, attachment_index)."""

	cache: Any
	key: Any
	token: str
	ttl_sec: int = ATTACHMENT_LOCK_TTL_SEC

	def still_owned(self) -> bool:
		try:
			return _decode_redis_value(self.cache.get(self.key)) == self.token
		except Exception:
			return False

	def renew(self) -> bool:
		"""Extend TTL if still owned. Returns False if ownership was lost."""
		try:
			raw = self.cache.eval(
				_RENEW_LOCK_IF_OWNER, 1, self.key, self.token, int(self.ttl_sec)
			)
			return raw == 1 or raw is True
		except Exception:
			return False

	def ensure_owned(self) -> None:
		"""Fail closed if ownership was lost (TTL expiry or stolen key)."""
		if not self.still_owned():
			raise AttachmentPersistLockLost("attachment persist lock ownership lost")

	def release(self) -> None:
		"""Release only if this token still owns the lock."""
		try:
			self.cache.eval(_RELEASE_LOCK_IF_OWNER, 1, self.key, self.token)
		except Exception:
			pass


def _sanitize_error(exc: BaseException) -> str:
	"""Short, safe error text — never include secrets, URLs, or payloads."""
	name = type(exc).__name__
	if isinstance(exc, ConnectorError):
		msg = str(exc.message if hasattr(exc, "message") else exc)
		msg = re.sub(r"https?://\S+", "[redacted]", msg, flags=re.I)
		msg = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+", r"\1=[redacted]", msg)
		return f"{name}: {msg}"[:240]
	return f"{name}: attachment persist failed"[:240]


def _attachment_type_from_mime(content_type: str) -> str:
	ct = (content_type or "").lower()
	if ct.startswith("image/"):
		return "image"
	if ct.startswith("video/"):
		return "video"
	if ct.startswith("audio/"):
		return "audio"
	return "file"


def _extension_for_mime(content_type: str) -> str:
	ext = mimetypes.guess_extension(content_type or "") or ".bin"
	if ext == ".jpe":
		ext = ".jpg"
	ext = "." + re.sub(r"[^A-Za-z0-9]", "", ext.lstrip("."))[:8]
	return ext or ".bin"


def _safe_filename(message_name: str, attachment_index: int, content_type: str) -> str:
	stem = _SAFE_STEM.sub("_", (message_name or "msg").strip())[:80] or "msg"
	return f"omni_{stem}_{int(attachment_index)}{_extension_for_mime(content_type)}"


def _sha256_hex(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def _result(
	*,
	ok: bool,
	message: str,
	attachment_index: int,
	status: str,
	file: str | None = None,
	content_hash: str | None = None,
	error: str | None = None,
	downloaded: bool = False,
	retryable: bool = False,
	lock_lost: bool = False,
) -> dict[str, Any]:
	# Intentionally omit file_url / CDN URLs.
	return {
		"ok": ok,
		"message": message,
		"attachment_index": attachment_index,
		"status": status,
		"file": file,
		"content_hash": content_hash,
		"error": error,
		"downloaded": downloaded,
		"retryable": retryable,
		"lock_lost": lock_lost,
	}


def is_lock_lost_result(result: dict[str, Any] | None) -> bool:
	"""True when persistence stopped because Redis lock ownership was lost."""
	return bool(result and result.get("lock_lost"))


@contextmanager
def attachment_persist_lock(
	message_name: str, attachment_index: int
) -> Iterator[AttachmentPersistLock | None]:
	"""Non-blocking Redis lock. Yields an owned handle, or ``None`` if busy/unavailable.

	Uses ``frappe.cache().set(..., nx=True, ex=TTL)`` with site-prefixed
	``make_key`` and tokenized Lua release. Never deletes another worker's lock.
	"""
	logical = attachment_persist_lock_key(message_name, attachment_index)
	token = secrets.token_hex(16)
	lock: AttachmentPersistLock | None = None
	try:
		cache = frappe.cache()
		key = cache.make_key(logical) if hasattr(cache, "make_key") else logical
		raw = cache.set(key, token, nx=True, ex=ATTACHMENT_LOCK_TTL_SEC)
		if raw is True or raw == 1:
			lock = AttachmentPersistLock(cache=cache, key=key, token=token)
	except Exception:
		lock = None

	try:
		yield lock
	finally:
		if lock is not None:
			lock.release()


# Backwards-compatible private alias (tests / older call sites).
_attachment_persist_lock = attachment_persist_lock


def _load_message(message_name: str):
	"""Load message without ``FOR UPDATE`` (no long DB row lock)."""
	if not message_name or not frappe.db.exists(MESSAGE_DOCTYPE, message_name):
		frappe.throw(
			_("Omnichannel Message {0} not found").format(message_name),
			frappe.ValidationError,
		)
	return frappe.get_doc(MESSAGE_DOCTYPE, message_name)


def _resolve_facebook_page_id(message) -> tuple[str, str]:
	"""Return ``(page_id, external_message_id)`` or throw."""
	if (message.provider or "").strip() != PROVIDER:
		frappe.throw(
			_("Attachment persist requires a Facebook Messenger message"),
			frappe.ValidationError,
		)
	external_id = (message.external_message_id or "").strip()
	if not external_id:
		frappe.throw(
			_("Message has no external_message_id; cannot fetch attachment"),
			frappe.ValidationError,
		)
	conversation = (message.conversation or "").strip()
	if not conversation:
		frappe.throw(_("Message has no conversation"), frappe.ValidationError)
	meta = frappe.db.get_value(
		"Omnichannel Conversation",
		conversation,
		["page_id", "provider"],
		as_dict=True,
	)
	if not meta:
		frappe.throw(_("Conversation {0} not found").format(conversation), frappe.ValidationError)
	if (meta.provider or "").strip() != PROVIDER:
		frappe.throw(
			_("Conversation is not a Facebook Messenger thread"),
			frappe.ValidationError,
		)
	page_id = (meta.page_id or "").strip()
	if not page_id:
		frappe.throw(_("Conversation has no page_id"), frappe.ValidationError)
	return page_id, external_id


def _find_child_row(message, attachment_index: int):
	for row in message.get("attachments") or []:
		if cint(row.attachment_index) == attachment_index:
			return row
	return None


def _get_child_row(message_name: str, attachment_index: int):
	"""Targeted child lookup (no full parent save)."""
	rows = frappe.get_all(
		CHILD_DOCTYPE,
		filters={
			"parent": message_name,
			"parenttype": MESSAGE_DOCTYPE,
			"attachment_index": attachment_index,
		},
		fields=[
			"name",
			"attachment_index",
			"status",
			"file",
			"content_hash",
			"mime_type",
			"attachment_type",
			"error_summary",
		],
		limit_page_length=1,
	)
	return rows[0] if rows else None


def _physical_file_exists(file_name: str) -> bool:
	"""True when the File row resolves to an existing private filesystem path."""
	try:
		path = get_file_path(file_name)
	except Exception:
		return False
	if not path:
		return False
	return os.path.exists(path)


def _file_is_valid_private(file_name: str | None, message_name: str) -> bool:
	"""Require private URL, system owner, correct attachment target, and disk bytes."""
	if not file_name:
		return False
	meta = frappe.db.get_value(
		"File",
		file_name,
		[
			"name",
			"is_private",
			"owner",
			"file_url",
			"attached_to_doctype",
			"attached_to_name",
		],
		as_dict=True,
	)
	if not meta:
		return False
	if cint(meta.is_private) != 1:
		return False
	if (meta.owner or "") != SYSTEM_OWNER:
		return False
	if (meta.attached_to_doctype or "") != MESSAGE_DOCTYPE:
		return False
	if (meta.attached_to_name or "") != message_name:
		return False
	file_url = meta.file_url or ""
	if not file_url.startswith("/private/files/"):
		return False
	return _physical_file_exists(file_name)


def _ensure_system_owned_private(file_doc, message_name: str) -> None:
	"""Require a private File; only adjust owner. Never flip public→private in DB."""
	if cint(getattr(file_doc, "is_private", 0)) != 1:
		frappe.throw(_("Refusing to keep a public attachment File"), frappe.ValidationError)
	file_url = getattr(file_doc, "file_url", None) or ""
	if not str(file_url).startswith("/private/files/"):
		frappe.throw(_("Attachment File must use a private file URL"), frappe.ValidationError)
	if (getattr(file_doc, "attached_to_doctype", None) or "") != MESSAGE_DOCTYPE:
		frappe.throw(_("Attachment File has wrong attached_to_doctype"), frappe.ValidationError)
	if (getattr(file_doc, "attached_to_name", None) or "") != message_name:
		frappe.throw(_("Attachment File has wrong attached_to_name"), frappe.ValidationError)
	if (getattr(file_doc, "owner", None) or "") != SYSTEM_OWNER:
		frappe.db.set_value(
			"File",
			file_doc.name,
			{"owner": SYSTEM_OWNER},
			update_modified=False,
		)
		file_doc.owner = SYSTEM_OWNER
	if not _physical_file_exists(file_doc.name):
		frappe.throw(_("Attachment File is missing on disk"), frappe.ValidationError)


def _find_existing_file_for_hash(message_name: str, content_hash: str) -> str | None:
	"""Reuse a private File already linked to this message with the same SHA-256.

	Frappe's own File.content_hash is MD5; we match via child rows we wrote.
	"""
	rows = frappe.get_all(
		CHILD_DOCTYPE,
		filters={
			"parent": message_name,
			"parenttype": MESSAGE_DOCTYPE,
			"content_hash": content_hash,
			"status": "Ready",
		},
		fields=["file", "name"],
		limit_page_length=20,
	)
	for row in rows:
		if _file_is_valid_private(row.file, message_name):
			return row.file
	return None


def _next_child_idx(message_name: str) -> int:
	rows = frappe.get_all(
		CHILD_DOCTYPE,
		filters={"parent": message_name, "parenttype": MESSAGE_DOCTYPE},
		fields=["idx"],
		order_by="idx desc",
		limit_page_length=1,
	)
	if not rows:
		return 1
	return cint(rows[0].idx) + 1


def _ensure_pending_child(message_name: str, attachment_index: int, existing_row) -> str:
	"""Create or reset the logical child row via insert/set_value only."""
	if existing_row is not None and getattr(existing_row, "name", None):
		frappe.db.set_value(
			CHILD_DOCTYPE,
			existing_row.name,
			{"status": "Pending", "error_summary": ""},
			update_modified=False,
		)
		return existing_row.name

	child = frappe.get_doc(
		{
			"doctype": CHILD_DOCTYPE,
			"parent": message_name,
			"parenttype": MESSAGE_DOCTYPE,
			"parentfield": CHILD_PARENTFIELD,
			"idx": _next_child_idx(message_name),
			"attachment_index": attachment_index,
			"status": "Pending",
		}
	)
	child.insert(ignore_permissions=True)
	return child.name


def _mark_child_ready(
	child_name: str,
	*,
	attachment_type: str,
	mime_type: str,
	file_name: str,
	content_hash: str,
) -> None:
	frappe.db.set_value(
		CHILD_DOCTYPE,
		child_name,
		{
			"attachment_type": attachment_type,
			"mime_type": mime_type,
			"file": file_name,
			"content_hash": content_hash,
			"status": "Ready",
			"error_summary": "",
		},
		update_modified=False,
	)


def _mark_child_failed(child_name: str | None, error_summary: str) -> None:
	if not child_name:
		frappe.throw(_("Failed to record attachment failure status"), frappe.ValidationError)
	current_file = frappe.db.get_value(CHILD_DOCTYPE, child_name, "file")
	values: dict[str, Any] = {
		"status": "Failed",
		"error_summary": (error_summary or "")[:240],
	}
	parent = frappe.db.get_value(CHILD_DOCTYPE, child_name, "parent")
	if not _file_is_valid_private(current_file, parent or ""):
		values["file"] = None
	frappe.db.set_value(CHILD_DOCTYPE, child_name, values, update_modified=False)


def _delete_new_file_best_effort(file_name: str | None) -> None:
	"""Remove a File created in this attempt only (never pre-existing Files)."""
	if not file_name:
		return
	frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)


def _get_adapter() -> FacebookConnectorAdapter:
	return FacebookConnectorAdapter()


def is_attachment_ready(message_name: str, attachment_index: int) -> bool:
	"""True when the child row is Ready with a valid private File + hash."""
	row = _get_child_row(message_name, attachment_index)
	if row is None:
		return False
	return bool(
		(row.status or "") == "Ready"
		and _file_is_valid_private(row.file, message_name)
		and (row.content_hash or "").strip()
	)


def _safe_private_file_url(file_url: Any) -> str | None:
	"""Return a trusted ``/private/files/...`` path only (never filesystem paths)."""
	url = str(file_url or "").strip()
	if not url.startswith("/private/files/"):
		return None
	if url.startswith("//") or ".." in url or "\\" in url or "\0" in url:
		return None
	if any(ch.isspace() for ch in url):
		return None
	return url


def get_ready_attachment_file_url(
	message_name: str, attachment_index: int = 0
) -> str | None:
	"""Private File URL when Ready + valid; otherwise ``None`` (no Failed/Pending leak)."""
	message_name = (message_name or "").strip()
	if not message_name:
		return None
	index = _parse_attachment_index(attachment_index)
	row = _get_child_row(message_name, index)
	if row is None:
		return None
	if (row.status or "") != "Ready":
		return None
	if not (row.content_hash or "").strip():
		return None
	if not _file_is_valid_private(row.file, message_name):
		return None
	raw = frappe.db.get_value("File", row.file, "file_url")
	return _safe_private_file_url(raw)


def map_ready_attachment_file_urls(
	message_names: list[str], attachment_index: int = 0
) -> dict[str, str]:
	"""Batch map message name → trusted ``/private/files/...`` for Ready attachments."""
	index = _parse_attachment_index(attachment_index)
	names = [str(n).strip() for n in message_names if str(n or "").strip()]
	if not names:
		return {}
	rows = frappe.get_all(
		CHILD_DOCTYPE,
		filters={
			"parent": ["in", names],
			"parenttype": MESSAGE_DOCTYPE,
			"attachment_index": index,
			"status": "Ready",
		},
		fields=["parent", "file", "content_hash", "status"],
		# Unlimited: duplicate Ready children must not truncate other parents
		# (mixed image+PDF pages were missing some attachment_file_url rows).
		limit_page_length=0,
	)
	out: dict[str, str] = {}
	for row in rows:
		parent = (row.parent or "").strip()
		if not parent or parent in out or not (row.content_hash or "").strip():
			continue
		if not _file_is_valid_private(row.file, parent):
			continue
		url = _safe_private_file_url(frappe.db.get_value("File", row.file, "file_url"))
		if url:
			out[parent] = url
	return out


def _lock_lost_result(message_name: str, index: int, *, downloaded: bool) -> dict[str, Any]:
	return _result(
		ok=False,
		message=message_name,
		attachment_index=index,
		status="Pending",
		error="attachment persist lock ownership lost; retry",
		downloaded=downloaded,
		retryable=True,
		lock_lost=True,
	)


def persist_message_attachment_unlocked(
	message_name: str,
	attachment_index: int = 0,
	*,
	lock_handle: AttachmentPersistLock | None = None,
) -> dict[str, Any]:
	"""Lock-free persistence core. Caller must already hold ``attachment_persist_lock``.

	When ``lock_handle`` is provided, ownership is renewed immediately before and
	after the Connector HTTP call (bounded by ConnectorClient timeouts: connect 5s
	+ read 15s) and re-checked before Ready/Failed finalization.

	Only classified ``ConnectorError`` download failures become a Failed child
	status. Unexpected DB / File / infrastructure errors propagate to the caller.
	Ownership loss returns ``lock_lost=True`` without marking Failed.

	Limitation: Redis ownership checks are not atomic with MariaDB commits; the
	RQ runner must re-check ownership immediately before ``commit()``.
	"""
	index = _parse_attachment_index(attachment_index)
	message_name = (message_name or "").strip()
	message = _load_message(message_name)
	page_id, external_id = _resolve_facebook_page_id(message)

	row = _get_child_row(message_name, index)
	if row is None:
		row = _find_child_row(message, index)

	if (
		row
		and (row.status or "") == "Ready"
		and _file_is_valid_private(row.file, message_name)
		and (row.content_hash or "").strip()
	):
		return _result(
			ok=True,
			message=message_name,
			attachment_index=index,
			status="Ready",
			file=row.file,
			content_hash=row.content_hash,
			downloaded=False,
		)

	child_name = _ensure_pending_child(message_name, index, row)
	downloaded = False
	created_file_name: str | None = None

	try:
		if lock_handle is not None and not lock_handle.renew():
			raise AttachmentPersistLockLost("attachment persist lock ownership lost")

		adapter = _get_adapter()
		payload: AttachmentPayload = adapter.get_message_attachment(
			page_id=page_id,
			message_id=external_id,
			attachment_index=index,
		)
		downloaded = True
		if lock_handle is not None and not lock_handle.renew():
			raise AttachmentPersistLockLost("attachment persist lock ownership lost")

		content_hash = _sha256_hex(payload.content)
		content_type = payload.content_type
		attachment_type = _attachment_type_from_mime(content_type)

		file_name = _find_existing_file_for_hash(message_name, content_hash)
		if not file_name:
			fname = _safe_filename(message_name, index, content_type)
			file_doc = save_file(
				fname,
				payload.content,
				MESSAGE_DOCTYPE,
				message_name,
				is_private=1,
				df=CHILD_PARENTFIELD,
			)
			created_file_name = file_doc.name
			_ensure_system_owned_private(file_doc, message_name)
			file_name = file_doc.name

		if not _file_is_valid_private(file_name, message_name):
			frappe.throw(
				_("Saved File is missing or not a valid private attachment"),
				frappe.ValidationError,
			)

		if lock_handle is not None:
			lock_handle.ensure_owned()

		_mark_child_ready(
			child_name,
			attachment_type=attachment_type,
			mime_type=content_type,
			file_name=file_name,
			content_hash=content_hash,
		)
		created_file_name = None

		return _result(
			ok=True,
			message=message_name,
			attachment_index=index,
			status="Ready",
			file=file_name,
			content_hash=content_hash,
			downloaded=downloaded,
		)
	except AttachmentPersistLockLost:
		if created_file_name:
			_delete_new_file_best_effort(created_file_name)
		return _lock_lost_result(message_name, index, downloaded=downloaded)
	except ConnectorError as exc:
		# Classified Connector/download failures only — infrastructure errors propagate.
		safe = _sanitize_error(exc)
		try:
			if lock_handle is not None:
				lock_handle.ensure_owned()
			_mark_child_failed(child_name, safe)
		except AttachmentPersistLockLost:
			if created_file_name:
				_delete_new_file_best_effort(created_file_name)
			return _lock_lost_result(message_name, index, downloaded=downloaded)
		if created_file_name:
			_delete_new_file_best_effort(created_file_name)
		return _result(
			ok=False,
			message=message_name,
			attachment_index=index,
			status="Failed",
			file=None,
			content_hash=None,
			error=safe,
			downloaded=downloaded,
		)


# Private alias kept for existing tests and internal callers.
_persist_under_lock = persist_message_attachment_unlocked


def persist_message_attachment(
	message_name: str, attachment_index: int = 0
) -> dict[str, Any]:
	"""Download one attachment via Connector and store a private File.

	Acquires the Redis lock, then delegates to
	``persist_message_attachment_unlocked``. Does not commit/rollback — RQ
	runners that need commit-before-unlock must call the unlocked core inside
	``attachment_persist_lock`` themselves (see ``sync_jobs``).
	"""
	index = _parse_attachment_index(attachment_index)
	message_name = (message_name or "").strip()
	if not message_name:
		frappe.throw(_("Omnichannel Message {0} not found").format(message_name), frappe.ValidationError)

	with attachment_persist_lock(message_name, index) as lock:
		if lock is None:
			return _result(
				ok=False,
				message=message_name,
				attachment_index=index,
				status="Pending",
				error="attachment persist already in progress; retry",
				downloaded=False,
				retryable=True,
			)
		return persist_message_attachment_unlocked(
			message_name, index, lock_handle=lock
		)


def _attach_outbound_bytes_file(
	message_name: str,
	content: bytes,
	content_type: str,
	*,
	attachment_index: int = 0,
	content_required_message: str,
	mime_required_message: str,
) -> dict[str, Any]:
	"""Persist outbound bytes as a private File + Ready child row.

	Does not call the Connector or commit/rollback. Reuses an existing Ready File
	with the same content hash. Never returns ``file_url``.

	Disk names always use ``_safe_filename`` (never the original upload basename).
	Original names with spaces / ``#`` / ``..`` would produce File URLs that
	``_safe_private_file_url`` rejects or that break raw Open hrefs — images
	already used the safe stem; documents must match so mixed batches keep a
	working ``attachment_file_url``.
	"""
	message_name = (message_name or "").strip()
	index = _parse_attachment_index(attachment_index)
	if not message_name:
		frappe.throw(_("Omnichannel Message {0} not found").format(message_name), frappe.ValidationError)
	if not isinstance(content, (bytes, bytearray)) or not bytes(content):
		frappe.throw(content_required_message, frappe.ValidationError)

	payload = bytes(content)
	mime = (content_type or "").split(";", 1)[0].strip().lower()
	if not mime:
		frappe.throw(mime_required_message, frappe.ValidationError)

	_load_message(message_name)
	content_hash = _sha256_hex(payload)
	existing_file = _find_existing_file_for_hash(message_name, content_hash)
	row = _get_child_row(message_name, index)
	if (
		row is not None
		and (row.status or "") == "Ready"
		and (row.content_hash or "") == content_hash
		and _file_is_valid_private(row.file, message_name)
	):
		return _result(
			ok=True,
			message=message_name,
			attachment_index=index,
			status="Ready",
			file=row.file,
			content_hash=content_hash,
		)

	child_name = _ensure_pending_child(message_name, index, row)
	created_file_name: str | None = None
	try:
		file_name = existing_file
		if not file_name:
			fname = _safe_filename(message_name, index, mime)
			file_doc = save_file(
				fname,
				payload,
				MESSAGE_DOCTYPE,
				message_name,
				is_private=1,
				df=CHILD_PARENTFIELD,
			)
			created_file_name = file_doc.name
			_ensure_system_owned_private(file_doc, message_name)
			file_name = file_doc.name

		if not _file_is_valid_private(file_name, message_name):
			frappe.throw(
				_("Saved File is missing or not a valid private attachment"),
				frappe.ValidationError,
			)

		_mark_child_ready(
			child_name,
			attachment_type=_attachment_type_from_mime(mime),
			mime_type=mime,
			file_name=file_name,
			content_hash=content_hash,
		)
		created_file_name = None
		return _result(
			ok=True,
			message=message_name,
			attachment_index=index,
			status="Ready",
			file=file_name,
			content_hash=content_hash,
		)
	except Exception as exc:
		_mark_child_failed(child_name, _sanitize_error(exc))
		_delete_new_file_best_effort(created_file_name)
		raise


def attach_outbound_image_file(
	message_name: str,
	content: bytes,
	content_type: str,
	*,
	attachment_index: int = 0,
) -> dict[str, Any]:
	"""Persist outbound image bytes as a private File + Ready child row.

	Does not call the Connector or commit/rollback. Reuses an existing Ready File
	with the same content hash. Never returns ``file_url``.
	"""
	return _attach_outbound_bytes_file(
		message_name,
		content,
		content_type,
		attachment_index=attachment_index,
		content_required_message=_("Outbound image content is required"),
		mime_required_message=_("Outbound image MIME type is required"),
	)


def attach_outbound_document_file(
	message_name: str,
	content: bytes,
	content_type: str,
	*,
	filename: str | None = None,
	attachment_index: int = 0,
) -> dict[str, Any]:
	"""Persist outbound PDF/DOCX bytes as a private File + Ready child row.

	Does not call the Connector or commit/rollback. Reuses an existing Ready File
	with the same content hash. Never returns ``file_url``.
	"""
	mime = (content_type or "").split(";", 1)[0].strip().lower()
	if mime not in _OUTBOUND_DOCUMENT_MIME_TYPES:
		frappe.throw(
			_("Outbound document must be a PDF or DOCX file"),
			frappe.ValidationError,
		)
	if isinstance(content, (bytes, bytearray)) and len(bytes(content)) > MAX_OUTBOUND_DOCUMENT_BYTES:
		frappe.throw(_("Document exceeds 8 MiB limit"), frappe.ValidationError)

	# ``filename`` is accepted for API compatibility / Meta display upstream;
	# private File basename is always ``_safe_filename`` inside the helper.
	del filename
	return _attach_outbound_bytes_file(
		message_name,
		content,
		mime,
		attachment_index=attachment_index,
		content_required_message=_("Outbound document content is required"),
		mime_required_message=_("Outbound document MIME type is required"),
	)

"""Outbound Facebook text sending with local persistence and idempotency."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from typing import Iterator

import frappe
from frappe import _
from frappe.utils import now_datetime

from meta_omnichannel.facebook_messenger.adapter import (
	MAX_OUTBOUND_IMAGE_BYTES,
	FacebookConnectorAdapter,
)
from meta_omnichannel.meta_connectors.errors import (
	ConnectorError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.utils.idempotency import (
	build_outbound_idempotency_key,
	build_outbound_image_idempotency_key,
)

_CONFIRMED_OK = frozenset({"Sent", "Delivered", "Read"})
_UNCERTAIN = frozenset({"Unknown", "Queued", "Pending"})
# Ambiguous outcomes: Meta may already have accepted the send.
_UNCERTAIN_EXCEPTIONS = (ConnectorUnreachableError, ConnectorServerError)
_IMAGE_SEND_LOCK_TTL_SEC = 120
_IMAGE_SEND_LOCK_PREFIX = "omnichannel:fb_send_img"
_RELEASE_LOCK_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""


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


def _detect_image_mime(content: bytes) -> str | None:
	"""Sniff PNG / JPEG / GIF magic bytes (ignore declared extension)."""
	if not content:
		return None
	if content.startswith(b"\x89PNG\r\n\x1a\n"):
		return "image/png"
	if content.startswith(b"\xff\xd8\xff"):
		return "image/jpeg"
	if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
		return "image/gif"
	return None


def _normalize_declared_image_mime(content_type: str | None) -> str:
	ct = (content_type or "").split(";", 1)[0].strip().lower()
	if ct == "image/jpg":
		return "image/jpeg"
	return ct


def _validate_outbound_image_bytes(
	image_bytes: bytes, *, content_type: str | None = None
) -> tuple[bytes, str]:
	"""Return ``(payload, detected_mime)`` or throw before any persistence."""
	if not isinstance(image_bytes, (bytes, bytearray)):
		frappe.throw(_("Image bytes are required"), frappe.ValidationError)
	payload = bytes(image_bytes)
	if not payload:
		frappe.throw(_("Image bytes are required"), frappe.ValidationError)
	if len(payload) > MAX_OUTBOUND_IMAGE_BYTES:
		frappe.throw(_("Image exceeds 8 MiB limit"), frappe.ValidationError)

	detected = _detect_image_mime(payload)
	if not detected:
		frappe.throw(_("Image must be a PNG, JPEG, or GIF"), frappe.ValidationError)

	declared = _normalize_declared_image_mime(content_type)
	if declared and declared != detected:
		frappe.throw(
			_("Image content does not match the declared MIME type"),
			frappe.ValidationError,
		)
	return payload, detected


def _load_image_reservation(idem: str):
	"""Load the unique nonce reservation row, including content hash."""
	return frappe.db.get_value(
		"Omnichannel Message",
		{"idempotency_key": idem},
		["name", "delivery_status", "error_summary", "raw_payload_hash"],
		as_dict=True,
	)


def _response_for_image_reservation(existing, *, content_sha: str) -> dict:
	"""Dedupe or conflict against a reserved nonce row (never calls Connector)."""
	stored = (getattr(existing, "raw_payload_hash", None) or "").strip().lower()
	incoming = (content_sha or "").strip().lower()
	if stored and incoming and stored != incoming:
		return _conflict_response(existing)
	payload = _response_for_existing(existing, deduplicated=True)
	payload["conflict"] = False
	return payload


@contextmanager
def _image_send_nonce_lock(
	conversation_name: str, client_nonce: str
) -> Iterator[tuple[bool, bool]]:
	"""Serialize sends for one conversation+nonce.

	Yields ``(owned, redis_available)``:
	- owned=True: this caller holds the Redis NX lock.
	- owned=False, redis_available=True: another worker holds the lock.
	- owned=False, redis_available=False: Redis failed — callers must rely on the
	  unique DB reservation key (fail closed only if that insert cannot be fenced).
	"""
	logical = f"{_IMAGE_SEND_LOCK_PREFIX}:{conversation_name}:{client_nonce}"
	token = secrets.token_hex(16)
	owned = False
	redis_available = True
	cache = key = None
	try:
		cache = frappe.cache()
		key = cache.make_key(logical) if hasattr(cache, "make_key") else logical
		raw = cache.set(key, token, nx=True, ex=_IMAGE_SEND_LOCK_TTL_SEC)
		owned = raw is True or raw == 1
	except Exception:
		owned = False
		redis_available = False
		cache = key = None

	try:
		yield owned, redis_available
	finally:
		if owned and cache is not None and key is not None:
			try:
				cache.eval(_RELEASE_LOCK_IF_OWNER, keys=[key], args=[token])
			except Exception:
				try:
					if cache.get(key) == token:
						cache.delete(key)
				except Exception:
					pass


def _conflict_response(existing) -> dict:
	return {
		"ok": False,
		"uncertain": False,
		"conflict": True,
		"deduplicated": False,
		"message": existing.name if existing else None,
		"delivery_status": (existing.delivery_status if existing else "Failed") or "Failed",
		"error": _("This nonce was already used with a different image"),
	}


def _in_progress_response() -> dict:
	return {
		"ok": False,
		"uncertain": True,
		"deduplicated": False,
		"conflict": False,
		"delivery_status": "Queued",
		"error": _("An image send with this nonce is already in progress"),
	}


def send_facebook_image(
	*,
	conversation_name: str,
	client_nonce: str,
	image_bytes: bytes,
	filename: str | None = None,
	content_type: str | None = None,
) -> dict:
	"""Send one outbound image via Connector multipart upload; persist private File first.

	Concurrency fence: unique ``idempotency_key`` is nonce-scoped (conversation + nonce).
	Image bytes are hashed into ``raw_payload_hash``. Redis NX is an accelerator; when
	Redis is down the unique key still prevents a second Connector call.
	"""
	client_nonce = (client_nonce or "").strip()
	if not client_nonce:
		frappe.throw(_("client_nonce is required"), frappe.ValidationError)

	payload, mime = _validate_outbound_image_bytes(image_bytes, content_type=content_type)
	content_sha = hashlib.sha256(payload).hexdigest()

	conv = frappe.get_doc("Omnichannel Conversation", conversation_name)
	if conv.provider != "Facebook":
		frappe.throw(_("Conversation provider does not support Facebook send"))

	idem = build_outbound_image_idempotency_key(
		conversation_name=conv.name,
		client_nonce=client_nonce,
		content_sha256=content_sha,
	)

	with _image_send_nonce_lock(conv.name, client_nonce) as (got_lock, redis_available):
		existing = _load_image_reservation(idem)
		if existing:
			return _response_for_image_reservation(existing, content_sha=content_sha)

		if not got_lock and redis_available:
			# Peer holds Redis lock — do not start a second Meta send.
			existing = _load_image_reservation(idem)
			if existing:
				return _response_for_image_reservation(existing, content_sha=content_sha)
			return _in_progress_response()

		# got_lock OR Redis unavailable: DB unique key is the authoritative fence.
		safe_name = (filename or "").strip().replace("\\", "_").replace("/", "_")
		if not safe_name:
			safe_name = f"image.{mime.split('/', 1)[1]}"

		msg = frappe.get_doc(
			{
				"doctype": "Omnichannel Message",
				"conversation": conv.name,
				"channel": conv.channel,
				"provider": "Facebook",
				"direction": "Outbound",
				"message_type": "Image",
				"text_content": "",
				"content": "",
				"idempotency_key": idem,
				"raw_payload_hash": content_sha,
				"delivery_status": "Queued",
				"sent_at": now_datetime(),
				"sender_external_id": conv.page_id,
				"attachment_mime": mime,
			}
		)
		try:
			msg.insert(ignore_permissions=True)
		except Exception as exc:
			if not _is_duplicate_idempotency_error(exc):
				raise
			frappe.db.rollback()
			existing = _load_image_reservation(idem)
			if not existing:
				raise
			return _response_for_image_reservation(existing, content_sha=content_sha)

		frappe.db.commit()

		try:
			# Lazy import: avoids pulling File helpers into text-only callers/tests.
			from meta_omnichannel.facebook_messenger import media as fb_media

			fb_media.attach_outbound_image_file(
				msg.name, payload, mime, attachment_index=0
			)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.set_value(
				"Omnichannel Message",
				msg.name,
				{
					"delivery_status": "Failed",
					"error_summary": str(exc)[:440],
				},
			)
			frappe.db.commit()
			return {
				"ok": False,
				"uncertain": False,
				"deduplicated": False,
				"conflict": False,
				"message": msg.name,
				"delivery_status": "Failed",
				"error": str(exc)[:440],
			}

		try:
			adapter = FacebookConnectorAdapter()
			result = adapter.send_image(
				page_id=conv.page_id,
				recipient_id=conv.participant_id,
				idempotency_key=idem,
				image_bytes=payload,
				filename=safe_name,
				content_type=mime,
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
				if not frappe.db.exists("Omnichannel Message", {"external_message_id": external_id}):
					values["external_message_id"] = str(external_id)
			frappe.db.set_value("Omnichannel Message", msg.name, values)
			frappe.db.set_value(
				"Omnichannel Conversation",
				conv.name,
				{
					"last_message_at": now_datetime(),
					"last_message_preview": _("Image")[:500],
				},
			)
			return {
				"ok": True,
				"uncertain": False,
				"deduplicated": bool(isinstance(result, dict) and result.get("duplicate")),
				"conflict": False,
				"message": msg.name,
				"delivery_status": "Sent",
			}
		except _UNCERTAIN_EXCEPTIONS as exc:
			frappe.db.set_value(
				"Omnichannel Message",
				msg.name,
				{"delivery_status": "Unknown", "error_summary": str(exc)[:440]},
			)
			return {
				"ok": False,
				"uncertain": True,
				"deduplicated": False,
				"conflict": False,
				"message": msg.name,
				"delivery_status": "Unknown",
				"error": str(exc),
			}
		except ConnectorError as exc:
			frappe.db.set_value(
				"Omnichannel Message",
				msg.name,
				{"delivery_status": "Failed", "error_summary": str(exc)[:440]},
			)
			return {
				"ok": False,
				"uncertain": False,
				"deduplicated": False,
				"conflict": False,
				"message": msg.name,
				"delivery_status": "Failed",
				"error": str(exc),
			}

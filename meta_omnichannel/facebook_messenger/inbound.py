"""Facebook Messenger inbound notification handling (Connector → Frappe).

HTTP handlers stay thin: authenticate, validate, resolve conversation locally,
enqueue existing message sync. Never call the Connector API from the request path.
Notifications are tip-only — they must not overwrite trusted conversation metadata.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import frappe
from frappe.utils.password import get_decrypted_password

from meta_omnichannel.facebook_messenger.sync import (
	PROVIDER,
	_ensure_facebook_channel,
)
from meta_omnichannel.jobs.sync_jobs import enqueue_conversation_message_sync
from meta_omnichannel.meta_connectors.inbound_hmac import (
	DEFAULT_MAX_SKEW_SEC,
	SIGNATURE_HEADER,
	TIMESTAMP_HEADER,
	verify_request_signature,
)
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.threading_keys import facebook_thread_key as thread_key

SETTINGS_DOCTYPE = "Omnichannel Settings"
INBOUND_SECRET_FIELD = "inbound_notification_secret"
REPLAY_CACHE_PREFIX = "omnichannel:inbound_notif:"
REPLAY_TTL_SEC = 600
# Supported Connector event types for Phase 1 (inbound customer messages only).
SUPPORTED_EVENT_TYPES = frozenset(
	{
		"message.inbound",
		"facebook.message.inbound",
	}
)
SUPPORTED_PROVIDERS = frozenset({"facebook", "facebook_messenger", "messenger"})
# Omnichannel Channel.status options: Draft | Connected | Disconnected | Error | Pending Connector Support
CONNECTED_CHANNEL_STATUS = "Connected"
REALTIME_EVENT = "omnichannel_conversation_updated"
# Lua: delete replay key only when ownership token still matches.
_RELEASE_REPLAY_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""


class InboundNotificationError(Exception):
	"""Auth / validation failure with HTTP-ish status and stable code."""

	def __init__(self, code: str, message: str, *, status: int = 401):
		super().__init__(message)
		self.code = code
		self.status = status
		self.message = message


def get_inbound_notification_secret() -> str | None:
	"""Return the per-site inbound HMAC secret, or None when unset."""
	secret = get_decrypted_password(
		SETTINGS_DOCTYPE,
		SETTINGS_DOCTYPE,
		INBOUND_SECRET_FIELD,
		raise_exception=False,
	)
	return (secret or "").strip() or None


def get_expected_tenant_id() -> str:
	return str(
		frappe.db.get_single_value(SETTINGS_DOCTYPE, "connector_tenant_id") or ""
	).strip()


def _header(name: str) -> str | None:
	value = frappe.get_request_header(name)
	if value:
		return value
	return frappe.get_request_header(name.lower())


def authenticate_inbound_request(*, body: bytes) -> None:
	"""Verify HMAC + timestamp freshness. Raises ``InboundNotificationError``."""
	secret = get_inbound_notification_secret()
	if not secret:
		raise InboundNotificationError(
			"secret_not_configured",
			"Inbound notification secret is not configured",
			status=503,
		)
	timestamp = _header(TIMESTAMP_HEADER)
	signature = _header(SIGNATURE_HEADER)
	ok, reason = verify_request_signature(
		secret=secret,
		timestamp=timestamp,
		signature_header=signature,
		body=body or b"",
		max_skew_sec=DEFAULT_MAX_SKEW_SEC,
	)
	if not ok:
		raise InboundNotificationError(reason, f"Authentication failed: {reason}", status=401)


def _replay_redis_key(notification_id: str) -> Any:
	cache = frappe.cache()
	logical = f"{REPLAY_CACHE_PREFIX}{notification_id}"
	if hasattr(cache, "make_key"):
		return cache.make_key(logical)
	return logical


def _claim_notification_id(notification_id: str) -> str | None:
	"""Atomically reserve ``notification_id``. Returns ownership token, or None if duplicate.

	Fails **closed** with retryable 503 when Redis cannot confirm the reservation.
	"""
	token = secrets.token_hex(16)
	try:
		cache = frappe.cache()
		key = _replay_redis_key(notification_id)
		if not hasattr(cache, "set"):
			raise RuntimeError("redis set unavailable")
		acquired = cache.set(key, token, nx=True, ex=REPLAY_TTL_SEC)
		if acquired is True or acquired == 1:
			return token
		if acquired is False or acquired is None:
			return None
		raise RuntimeError(f"unexpected redis set result: {acquired!r}")
	except InboundNotificationError:
		raise
	except Exception:
		get_logger().info("inbound_notification_replay_cache_unavailable")
		raise InboundNotificationError(
			"replay_store_unavailable",
			"Replay protection temporarily unavailable; retry later",
			status=503,
		)


def _release_notification_id(notification_id: str, token: str | None) -> None:
	"""Release reservation only if ``token`` still owns it."""
	if not token or not notification_id:
		return
	try:
		cache = frappe.cache()
		key = _replay_redis_key(notification_id)
		cache.eval(_RELEASE_REPLAY_IF_OWNER, 1, key, token)
	except Exception:
		get_logger().info(
			"inbound_notification_replay_release_failed notification_id=%s",
			notification_id,
		)


def facebook_page_is_linked(page_id: str) -> bool:
	"""True when page_id maps to an enabled, Connected Facebook Omnichannel Channel."""
	page_id = (page_id or "").strip()
	if not page_id:
		return False
	name = frappe.db.get_value(
		"Omnichannel Channel",
		{
			"provider": PROVIDER,
			"external_account_id": page_id,
			"is_enabled": 1,
			"status": CONNECTED_CHANNEL_STATUS,
		},
		"name",
	)
	return bool(name)


def _normalize_provider(value: Any) -> str:
	return str(value or "").strip().lower()


def _truthy(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	return str(value).strip().lower() in {"1", "true", "yes", "y"}


def classify_notification(payload: dict[str, Any]) -> tuple[str, str | None]:
	"""Return ``(action, reason)`` where action is accept|ignore|reject."""
	provider = _normalize_provider(payload.get("provider"))
	if not provider:
		return "ignore", "provider_required"
	if provider not in SUPPORTED_PROVIDERS:
		return "ignore", "unsupported_provider"

	event_type = str(payload.get("event_type") or payload.get("type") or "").strip().lower()
	if not event_type:
		return "ignore", "event_type_required"
	if event_type not in SUPPORTED_EVENT_TYPES:
		return "ignore", "unsupported_event_type"

	if _truthy(payload.get("is_echo")) or _truthy(payload.get("echo")):
		return "ignore", "echo"

	direction = str(payload.get("direction") or payload.get("message_direction") or "").strip().lower()
	if direction in {"outbound", "out", "sent"}:
		return "ignore", "outbound"

	return "accept", None


def validate_tenant_and_page(payload: dict[str, Any]) -> tuple[str, str]:
	"""Validate tenant/page/sender. Returns ``(page_id, sender_id)``."""
	expected_tenant = get_expected_tenant_id()
	if not expected_tenant:
		raise InboundNotificationError(
			"tenant_not_configured",
			"Connector tenant is not configured",
			status=503,
		)
	got_tenant = str(payload.get("tenant_id") or payload.get("tenant") or "").strip()
	if got_tenant != expected_tenant:
		raise InboundNotificationError(
			"tenant_mismatch",
			"Tenant does not match this site",
			status=403,
		)

	page_id = str(payload.get("page_id") or payload.get("pageId") or "").strip()
	if not page_id:
		raise InboundNotificationError(
			"page_id_required",
			"page_id is required",
			status=400,
		)
	if not facebook_page_is_linked(page_id):
		raise InboundNotificationError(
			"page_not_linked",
			"Facebook page is not linked or not Connected on this site",
			status=403,
		)

	sender_id = str(
		payload.get("sender_id")
		or payload.get("senderId")
		or payload.get("participant_id")
		or payload.get("psid")
		or ""
	).strip()
	if not sender_id:
		raise InboundNotificationError(
			"sender_id_required",
			"sender_id is required",
			status=400,
		)

	# Reject caller-supplied conversation / job routing — resolve locally only.
	if payload.get("conversation") or payload.get("conversation_name") or payload.get("job"):
		raise InboundNotificationError(
			"untrusted_routing_fields",
			"conversation/job fields are not accepted",
			status=400,
		)

	return page_id, sender_id


def resolve_conversation_for_notification(page_id: str, sender_id: str) -> str:
	"""Resolve or create conversation from validated ids only (tip-only, no metadata write).

	- Existing thread: return name; do **not** update participant name, preview, or timestamps.
	- New thread: insert minimum required fields; display name defaults to ``sender_id``.
	  Preview / last_message_at are left empty until message sync fills them.
	"""
	page_id = (page_id or "").strip()
	sender_id = (sender_id or "").strip()
	if not page_id or not sender_id:
		raise InboundNotificationError(
			"conversation_resolve_failed",
			"Could not resolve conversation",
			status=400,
		)

	key = thread_key(page_id, sender_id)
	existing = frappe.db.get_value(
		"Omnichannel Conversation",
		{"external_thread_key": key},
		"name",
	)
	if existing:
		return str(existing)

	# Page already validated as Connected + enabled; ensure channel row for FK.
	channel = _ensure_facebook_channel(page_id)
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Conversation",
			"status": "Open",
			"channel": channel,
			"provider": PROVIDER,
			"external_thread_key": key,
			"page_id": page_id,
			"participant_id": sender_id,
			# Do not trust notification display names — sync path can refine later.
			"participant_name": sender_id,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def enqueue_inbound_message_sync(conversation_name: str) -> str | None:
	"""Enqueue existing per-conversation message sync (deduplicated by job name)."""
	return enqueue_conversation_message_sync(conversation_name)


def _candidate_omnichannel_users() -> list[str]:
	"""Return role-holder candidates without scanning every User row.

	Uses ``Has Role`` only (bounded by Omnichannel roles), plus Administrator.
	Frappe v15 ``get_all`` defaults ``limit_page_length=0`` (no LIMIT); we set it
	explicitly. ``distinct=True`` is supported by DatabaseQuery on v15.
	"""
	from meta_omnichannel.permissions import AGENT_ROLES

	candidates: set[str] = set()
	try:
		# limit_page_length=0 → unlimited (same as get_all default on v15).
		# Do not use get_list (defaults to 20) or omit this on older call sites.
		rows = frappe.get_all(
			"Has Role",
			filters={"role": ["in", list(AGENT_ROLES)], "parenttype": "User"},
			pluck="parent",
			distinct=True,
			limit_page_length=0,
		)
		for name in rows or []:
			if name:
				candidates.add(str(name))
	except Exception:
		# Fail closed: do not fall back to Administrator-only publish.
		get_logger().info("omnichannel_realtime_candidate_lookup_failed")
		raise

	candidates.add("Administrator")
	return sorted(candidates)


def _user_is_enabled(user: str) -> bool:
	if not user or user == "Guest":
		return False
	try:
		return bool(frappe.db.get_value("User", user, "enabled"))
	except Exception:
		return False


def _eligible_realtime_recipients(doc) -> list[str]:
	"""Users allowed to see this conversation, using existing permission helpers."""
	from meta_omnichannel.permissions import (
		has_conversation_permission,
		user_is_omnichannel_agent,
	)

	recipients: list[str] = []
	for user in _candidate_omnichannel_users():
		if not _user_is_enabled(user):
			continue
		if not user_is_omnichannel_agent(user):
			continue
		if not has_conversation_permission(doc, "read", user):
			continue
		recipients.append(user)
	return recipients


def publish_conversation_updated(
	*,
	conversation: str,
	processed: int | None = None,
	source: str = "message_sync",
) -> None:
	"""Permission-scoped realtime ping (per authorized user; never site-wide).

	Payload is non-sensitive identifiers only. Failures never raise to the caller
	so message sync success is preserved.
	"""
	conversation = (conversation or "").strip()
	if not conversation:
		return

	data = {
		"conversation": conversation,
		"provider": PROVIDER,
		"source": source,
	}
	if processed is not None:
		data["processed"] = int(processed)

	try:
		if not frappe.db.exists("Omnichannel Conversation", conversation):
			return
		doc = frappe.get_doc("Omnichannel Conversation", conversation)
		recipients = _eligible_realtime_recipients(doc)
	except Exception:
		# Never fall back to site-wide broadcast on lookup/auth discovery errors.
		get_logger().info(
			"omnichannel_realtime_publish_failed conversation=%s",
			conversation,
		)
		return

	for user in recipients:
		try:
			# Always pass user= so Frappe targets user:{name}, never room "all".
			frappe.publish_realtime(
				REALTIME_EVENT,
				data,
				user=user,
				after_commit=True,
			)
		except Exception:
			# Isolate per-recipient failures; continue other users.
			get_logger().info(
				"omnichannel_realtime_publish_user_failed conversation=%s",
				conversation,
			)


def process_facebook_inbound_notification(*, body: bytes) -> dict[str, Any]:
	"""Authenticate, validate, resolve conversation, enqueue sync. No Connector HTTP.

	Always parses the exact ``body`` bytes that passed HMAC verification.
	"""
	authenticate_inbound_request(body=body)

	try:
		payload = json.loads((body or b"").decode("utf-8") or "{}")
	except (UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise InboundNotificationError(
			"invalid_json",
			"Request body must be JSON",
			status=400,
		) from exc
	if not isinstance(payload, dict):
		raise InboundNotificationError(
			"invalid_json",
			"Request body must be a JSON object",
			status=400,
		)

	action, reason = classify_notification(payload)
	if action == "ignore":
		return {"ok": True, "ignored": True, "reason": reason}

	notification_id = str(payload.get("notification_id") or "").strip()
	if not notification_id:
		raise InboundNotificationError(
			"notification_id_required",
			"notification_id is required",
			status=400,
		)

	page_id, sender_id = validate_tenant_and_page(payload)

	claim_token = _claim_notification_id(notification_id)
	if not claim_token:
		return {
			"ok": True,
			"duplicate": True,
			"notification_id": notification_id,
		}

	try:
		conversation = resolve_conversation_for_notification(page_id, sender_id)
		job = enqueue_inbound_message_sync(conversation)
	except InboundNotificationError:
		_release_notification_id(notification_id, claim_token)
		raise
	except Exception as exc:
		_release_notification_id(notification_id, claim_token)
		get_logger().info(
			"facebook_inbound_notification_enqueue_failed notification_id=%s",
			notification_id,
		)
		raise InboundNotificationError(
			"enqueue_failed",
			"Failed to queue message sync; retry later",
			status=503,
		) from exc

	get_logger().info(
		"facebook_inbound_notification_enqueued conversation=%s notification_id=%s",
		conversation,
		notification_id,
	)
	return {
		"ok": True,
		"queued": True,
		"conversation": conversation,
		"notification_id": notification_id,
		# Serialize RQ Job id only (commit 921e17f) — never return the Job object.
		"job": job.id if job is not None else None,
	}

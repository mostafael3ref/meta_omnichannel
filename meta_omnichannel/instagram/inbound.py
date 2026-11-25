"""Instagram inbound notification handling (Connector → Frappe).

Reuses Facebook inbound security primitives (HMAC, timestamp skew, Redis replay,
tenant check). Resolves Instagram conversations tip-only and enqueues the
Instagram-only message sync job — **not** the Facebook ``run_conversation_message_sync``
path. Real-Time publish happens only after that job's successful sync via the
shared permission-scoped publisher.
"""

from __future__ import annotations

import json
from typing import Any

import frappe

from meta_omnichannel.facebook_messenger.inbound import (
	CONNECTED_CHANNEL_STATUS,
	InboundNotificationError,
	_claim_notification_id,
	_release_notification_id,
	authenticate_inbound_request,
	get_expected_tenant_id,
)
from meta_omnichannel.instagram.sync import PROVIDER, _ensure_instagram_channel
from meta_omnichannel.jobs.sync_jobs import enqueue_instagram_conversation_message_sync
from meta_omnichannel.utils.logger import get_logger
from meta_omnichannel.utils.threading_keys import instagram_thread_key

# Supported Connector event types for Instagram inbound tips.
SUPPORTED_EVENT_TYPES = frozenset(
	{
		"message.inbound",
		"instagram.message.inbound",
	}
)
SUPPORTED_PROVIDERS = frozenset({"instagram", "instagram_messaging", "ig"})


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


def instagram_account_is_linked(account_id: str) -> bool:
	"""True when account_id maps to an enabled, Connected Instagram Omnichannel Channel."""
	account_id = (account_id or "").strip()
	if not account_id:
		return False
	name = frappe.db.get_value(
		"Omnichannel Channel",
		{
			"provider": PROVIDER,
			"external_account_id": account_id,
			"is_enabled": 1,
			"status": CONNECTED_CHANNEL_STATUS,
		},
		"name",
	)
	return bool(name)


def validate_tenant_and_account(payload: dict[str, Any]) -> tuple[str, str]:
	"""Validate tenant/account/sender. Returns ``(account_id, sender_id)``."""
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

	# Primary field is account_id; page_id accepted as alias (DocType stores it in page_id).
	account_id = str(
		payload.get("account_id")
		or payload.get("accountId")
		or payload.get("page_id")
		or payload.get("pageId")
		or ""
	).strip()
	if not account_id:
		raise InboundNotificationError(
			"account_id_required",
			"account_id is required",
			status=400,
		)
	if not instagram_account_is_linked(account_id):
		raise InboundNotificationError(
			"account_not_linked",
			"Instagram account is not linked or not Connected on this site",
			status=403,
		)

	sender_id = str(
		payload.get("sender_id")
		or payload.get("senderId")
		or payload.get("participant_id")
		or payload.get("igsid")
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

	return account_id, sender_id


def resolve_conversation_for_notification(account_id: str, sender_id: str) -> str:
	"""Resolve or create Instagram conversation from validated ids only (tip-only).

	- Existing thread: return name; do **not** update participant name, preview, or timestamps.
	- New thread: insert minimum required fields; display name defaults to ``sender_id``.
	"""
	account_id = (account_id or "").strip()
	sender_id = (sender_id or "").strip()
	if not account_id or not sender_id:
		raise InboundNotificationError(
			"conversation_resolve_failed",
			"Could not resolve conversation",
			status=400,
		)

	key = instagram_thread_key(account_id, sender_id)
	existing = frappe.db.get_value(
		"Omnichannel Conversation",
		{"external_thread_key": key},
		"name",
	)
	if existing:
		return str(existing)

	channel = _ensure_instagram_channel(account_id)
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Conversation",
			"status": "Open",
			"channel": channel,
			"provider": PROVIDER,
			"external_thread_key": key,
			# DocType field is named page_id; stores the Instagram account id.
			"page_id": account_id,
			"participant_id": sender_id,
			# Do not trust notification display names — sync path can refine later.
			"participant_name": sender_id,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def enqueue_inbound_message_sync(conversation_name: str):
	"""Enqueue Instagram per-conversation message sync (deduplicated).

	Real-Time is published by the worker only after a successful sync.
	"""
	return enqueue_instagram_conversation_message_sync(conversation_name)


def process_instagram_inbound_notification(*, body: bytes) -> dict[str, Any]:
	"""Authenticate, validate, resolve conversation, enqueue IG sync. No Connector HTTP.

	Always parses the exact ``body`` bytes that passed HMAC verification.
	Does not publish ``omnichannel_conversation_updated``.
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

	account_id, sender_id = validate_tenant_and_account(payload)

	claim_token = _claim_notification_id(notification_id)
	if not claim_token:
		return {
			"ok": True,
			"duplicate": True,
			"notification_id": notification_id,
		}

	try:
		conversation = resolve_conversation_for_notification(account_id, sender_id)
		job = enqueue_inbound_message_sync(conversation)
	except InboundNotificationError:
		_release_notification_id(notification_id, claim_token)
		raise
	except Exception as exc:
		_release_notification_id(notification_id, claim_token)
		get_logger().info(
			"instagram_inbound_notification_enqueue_failed notification_id=%s",
			notification_id,
		)
		raise InboundNotificationError(
			"enqueue_failed",
			"Failed to queue message sync; retry later",
			status=503,
		) from exc

	get_logger().info(
		"instagram_inbound_notification_enqueued conversation=%s notification_id=%s",
		conversation,
		notification_id,
	)
	return {
		"ok": True,
		"queued": True,
		"conversation": conversation,
		"notification_id": notification_id,
		# Serialize RQ Job id only — never return the Job object.
		"job": job.id if job is not None else None,
	}

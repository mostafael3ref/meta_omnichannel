"""Background jobs and scheduled sync (bounded; retry-safe)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from meta_omnichannel.utils.logger import get_logger

# Match ``ATTACHMENT_LOCK_TTL_SEC`` (900). Frappe default ``long`` queue is 1500s;
# keep job timeout == lock TTL so a killed worker cannot outlive its lock claim.
FACEBOOK_ATTACHMENT_JOB_TIMEOUT_SEC = 900


def enqueue_facebook_conversation_sync(limit: int = 50):
	"""Enqueue a bounded Facebook inbox sync (conversations + recent messages)."""
	return frappe.enqueue(
		"meta_omnichannel.jobs.sync_jobs.run_facebook_conversation_sync",
		queue="short",
		timeout=600,
		limit=limit,
		job_id="omnichannel_facebook_conversation_sync",
		deduplicate=True,
	)


def run_facebook_conversation_sync(limit: int = 50) -> dict:
	"""Run the Facebook inbox coordinator (list + bounded message import)."""
	from meta_omnichannel.facebook_messenger.sync import sync_facebook_inbox_bounded

	result = sync_facebook_inbox_bounded(limit=limit)
	messages = result.get("messages") or {}
	get_logger().info(
		"scheduled_facebook_sync ok=%s skipped=%s processed=%s messages_synced=%s messages_failed=%s",
		result.get("ok"),
		result.get("skipped"),
		result.get("processed"),
		messages.get("synced"),
		len(messages.get("failed") or []),
	)
	return result


def enqueue_conversation_message_sync(conversation: str):
	return frappe.enqueue(
		"meta_omnichannel.jobs.sync_jobs.run_conversation_message_sync",
		queue="short",
		timeout=300,
		conversation=conversation,
		job_id=f"omnichannel_msg_sync:{conversation}",
		deduplicate=True,
	)


def run_conversation_message_sync(conversation: str) -> dict:
	from meta_omnichannel.facebook_messenger.inbound import publish_conversation_updated
	from meta_omnichannel.facebook_messenger.sync import sync_conversation_messages

	result = sync_conversation_messages(conversation)
	# Near-real-time Inbox refresh: non-sensitive identifiers only.
	if result.get("ok"):
		publish_conversation_updated(
			conversation=conversation,
			processed=result.get("processed"),
			source="message_sync",
		)
	return result


def enqueue_facebook_attachment_persist(
	message_name: str, attachment_index: int = 0
):
	"""Enqueue one Facebook attachment persist job (deduplicated).

	Skips enqueue when the attachment is already Ready with a valid private File.
	Returns ``None`` when skipped; otherwise the RQ Job (or direct call result).
	"""
	from meta_omnichannel.facebook_messenger import media as fb_media

	index = fb_media._parse_attachment_index(attachment_index)
	message_name = (message_name or "").strip()
	if not message_name:
		frappe.throw(
			_("Omnichannel Message {0} not found").format(message_name),
			frappe.ValidationError,
		)
	if not frappe.db.exists(fb_media.MESSAGE_DOCTYPE, message_name):
		frappe.throw(
			_("Omnichannel Message {0} not found").format(message_name),
			frappe.ValidationError,
		)
	if fb_media.is_attachment_ready(message_name, index):
		get_logger().info(
			"facebook_attachment_persist_skip_ready message=%s index=%s",
			message_name,
			index,
		)
		return None

	return frappe.enqueue(
		"meta_omnichannel.jobs.sync_jobs.run_facebook_attachment_persist",
		queue="long",
		timeout=FACEBOOK_ATTACHMENT_JOB_TIMEOUT_SEC,
		message_name=message_name,
		attachment_index=index,
		job_id=fb_media.attachment_persist_job_id(message_name, index),
		deduplicate=True,
	)


def run_facebook_attachment_persist(
	message_name: str, attachment_index: int = 0
) -> dict[str, Any]:
	"""RQ runner: Redis lock → unlocked persist → commit/rollback → unlock.

	Does **not** call ``persist_message_attachment`` (avoids nested locking).
	Owns the transaction boundary so Ready/Failed rows are visible before unlock.

	Lock-loss / ownership failure always rolls back and returns retryable (never
	success, never committed Failed). Ordinary Connector download failures may
	commit Failed while ownership is still valid.

	Limitation: ``still_owned()`` immediately before ``commit()`` is not an
	atomic Redis+MariaDB operation; a theoretical race remains between the
	ownership check and commit.
	"""
	from meta_omnichannel.facebook_messenger import media as fb_media

	index = fb_media._parse_attachment_index(attachment_index)
	message_name = (message_name or "").strip()
	if not message_name:
		frappe.throw(
			_("Omnichannel Message {0} not found").format(message_name),
			frappe.ValidationError,
		)

	with fb_media.attachment_persist_lock(message_name, index) as lock:
		if lock is None:
			return fb_media._result(
				ok=False,
				message=message_name,
				attachment_index=index,
				status="Pending",
				error="attachment persist already in progress; retry",
				downloaded=False,
				retryable=True,
				lock_lost=False,
			)
		try:
			result = fb_media.persist_message_attachment_unlocked(
				message_name, index, lock_handle=lock
			)
			# Lock-loss from the core: never commit partial Pending/Ready/Failed.
			if fb_media.is_lock_lost_result(result):
				frappe.db.rollback()
				return result
			# Re-check ownership immediately before commit (not atomic with MariaDB).
			if not lock.still_owned():
				frappe.db.rollback()
				return fb_media._lock_lost_result(
					message_name, index, downloaded=bool(result.get("downloaded"))
				)
			# Commit handled Ready / Failed outcomes while we still appear to own the lock.
			frappe.db.commit()
			return result
		except Exception:
			frappe.db.rollback()
			raise


def scheduled_facebook_sync() -> None:
	"""Hourly-long scheduler entrypoint (hooks.scheduler_events).

	Uses the same bounded coordinator as manual Sync. Hourly cadence balances
	API load and worker overlap; do not switch to aggressive sub-minute polling
	without reviewing Connector quotas and queue capacity.
	"""
	try:
		run_facebook_conversation_sync(limit=50)
	except Exception:
		get_logger().exception("scheduled_facebook_sync_failed")

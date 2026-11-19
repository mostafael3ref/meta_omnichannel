"""Background jobs and scheduled sync (bounded; retry-safe)."""

from __future__ import annotations

import frappe

from meta_omnichannel.utils.logger import get_logger


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

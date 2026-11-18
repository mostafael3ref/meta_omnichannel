"""Background jobs and scheduled sync (bounded; retry-safe)."""

from __future__ import annotations

import frappe

from meta_omnichannel.utils.logger import get_logger


def enqueue_facebook_conversation_sync(limit: int = 50) -> str:
	"""Enqueue a bounded Facebook conversation sync job."""
	return frappe.enqueue(
		"meta_omnichannel.jobs.sync_jobs.run_facebook_conversation_sync",
		queue="short",
		timeout=300,
		limit=limit,
		job_name="omnichannel_facebook_conversation_sync",
		deduplicate=True,
	)


def run_facebook_conversation_sync(limit: int = 50) -> dict:
	from meta_omnichannel.facebook_messenger.sync import sync_facebook_conversations

	result = sync_facebook_conversations(limit=limit)
	get_logger().info(
		"scheduled_facebook_sync ok=%s processed=%s",
		result.get("ok"),
		result.get("processed"),
	)
	return result


def enqueue_conversation_message_sync(conversation: str) -> str:
	return frappe.enqueue(
		"meta_omnichannel.jobs.sync_jobs.run_conversation_message_sync",
		queue="short",
		timeout=300,
		conversation=conversation,
		job_name=f"omnichannel_msg_sync:{conversation}",
		deduplicate=True,
	)


def run_conversation_message_sync(conversation: str) -> dict:
	from meta_omnichannel.facebook_messenger.sync import sync_conversation_messages

	return sync_conversation_messages(conversation)


def scheduled_facebook_sync() -> None:
	"""Hourly-ish sync entrypoint from hooks.scheduler_events."""
	try:
		run_facebook_conversation_sync(limit=50)
	except Exception:
		get_logger().exception("scheduled_facebook_sync_failed")

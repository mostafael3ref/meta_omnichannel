"""Stable thread key helpers (pure; no Frappe import)."""

from __future__ import annotations


def facebook_thread_key(page_id: str, sender_id: str) -> str:
	return f"{page_id}:{sender_id}"


def instagram_thread_key(account_id: str, sender_id: str) -> str:
	"""Stable key for an Instagram account ↔ participant thread."""
	return f"{account_id}:{sender_id}"

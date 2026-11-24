"""Instagram connector adapter — list conversations + message history.

Confirmed endpoints:
- ``GET /api/instagram/conversations``
- ``GET /api/instagram/conversations/{account_id}/{sender_id}/messages``

Outbound messaging remains disabled.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from meta_omnichannel.meta_connectors.client import ConnectorClient
from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorRateLimitError,
	ConnectorServerError,
)

CONVERSATIONS_PATH = "/api/instagram/conversations"

# Top-level envelope keys confirmed for the Instagram conversations list response.
CONFIRMED_ENVELOPE_KEYS = frozenset(
	{
		"tenant_id",
		"provider",
		"total",
		"conversations",
		"limit",
		"offset",
		"has_more",
		"next_offset",
	}
)

# Top-level envelope keys confirmed for Instagram conversation message history.
CONFIRMED_MESSAGES_ENVELOPE_KEYS = frozenset(
	{
		"tenant_id",
		"provider",
		"account_id",
		"sender_id",
		"total",
		"messages",
		"limit",
		"offset",
		"has_more",
		"next_offset",
	}
)

# Fields required on each message object (message_id and text may be null).
CONFIRMED_MESSAGE_FIELDS = (
	"direction",
	"text",
	"attachments",
	"timestamp",
	"message_id",
)

# Identity fields required on each non-empty conversation item for DocType sync.
REQUIRED_SYNC_ITEM_FIELDS = (
	"account_id",  # IG business account → Omnichannel Channel.external_account_id / Conversation.page_id
	"sender_id",  # Instagram-scoped user id → Conversation.participant_id / external_thread_key
)


def _raise_for_status(status_code: int, *, context: str) -> None:
	if status_code in (200, 201):
		return
	if status_code == 401:
		raise ConnectorAuthError(f"{context}: invalid or missing API key (HTTP 401)")
	if status_code == 403:
		raise ConnectorForbiddenError(f"{context}: authorization failure (HTTP 403)")
	if status_code == 429:
		raise ConnectorRateLimitError(f"{context}: rate limited (HTTP 429)")
	if 500 <= status_code <= 599:
		raise ConnectorServerError(f"{context}: server error (HTTP {status_code})")
	raise ConnectorError(f"{context}: unexpected HTTP {status_code}")


def validate_conversations_list_envelope(payload: Any) -> dict[str, Any]:
	"""Validate the confirmed list-response envelope (not per-item schema).

	Raises:
		ConnectorError: When the payload is not a dict, ``conversations`` is not a
			list, or required envelope keys are missing.
	"""
	if not isinstance(payload, dict):
		raise ConnectorError("list_conversations: response must be a JSON object")

	missing = sorted(CONFIRMED_ENVELOPE_KEYS - set(payload.keys()))
	if missing:
		raise ConnectorError(
			"list_conversations: missing envelope keys: " + ", ".join(missing)
		)

	conversations = payload.get("conversations")
	if not isinstance(conversations, list):
		raise ConnectorError("list_conversations: 'conversations' must be a list")

	for index, row in enumerate(conversations):
		if not isinstance(row, dict):
			raise ConnectorError(
				f"list_conversations: conversations[{index}] must be an object"
			)

	return payload


def validate_messages_envelope(payload: Any) -> dict[str, Any]:
	"""Validate the confirmed Instagram message-history envelope and item fields.

	``message_id`` and ``text`` may be null. Other confirmed message keys must be
	present; ``attachments`` must be a list when present (empty list allowed).

	Raises:
		ConnectorError: On malformed envelope or message objects.
	"""
	if not isinstance(payload, dict):
		raise ConnectorError("get_messages: response must be a JSON object")

	missing = sorted(CONFIRMED_MESSAGES_ENVELOPE_KEYS - set(payload.keys()))
	if missing:
		raise ConnectorError(
			"get_messages: missing envelope keys: " + ", ".join(missing)
		)

	messages = payload.get("messages")
	if not isinstance(messages, list):
		raise ConnectorError("get_messages: 'messages' must be a list")

	for index, row in enumerate(messages):
		if not isinstance(row, dict):
			raise ConnectorError(f"get_messages: messages[{index}] must be an object")

		row_missing = [field for field in CONFIRMED_MESSAGE_FIELDS if field not in row]
		if row_missing:
			raise ConnectorError(
				f"get_messages: messages[{index}] missing fields: "
				+ ", ".join(row_missing)
			)

		message_id = row.get("message_id")
		if message_id is not None and not isinstance(message_id, str):
			raise ConnectorError(
				f"get_messages: messages[{index}].message_id must be a string or null"
			)

		text = row.get("text")
		if text is not None and not isinstance(text, str):
			raise ConnectorError(
				f"get_messages: messages[{index}].text must be a string or null"
			)

		attachments = row.get("attachments")
		if not isinstance(attachments, list):
			raise ConnectorError(
				f"get_messages: messages[{index}].attachments must be a list"
			)

	return payload


def conversation_items_ready_for_sync(payload: dict[str, Any]) -> tuple[bool, list[str]]:
	"""Return whether every conversation item exposes fields needed for DocType sync.

	An empty ``conversations`` list is valid (zero rows to import). Non-empty items
	must include exact ``account_id`` and ``sender_id`` values — no aliases.
	"""
	rows = payload.get("conversations") or []
	if not rows:
		return True, []

	missing: set[str] = set()
	for row in rows:
		if not isinstance(row, dict):
			return False, list(REQUIRED_SYNC_ITEM_FIELDS)
		for field in REQUIRED_SYNC_ITEM_FIELDS:
			value = row.get(field)
			if value is None or (isinstance(value, str) and not value.strip()):
				missing.add(field)
	return (not missing), sorted(missing)


class InstagramConnectorAdapter:
	"""Typed client for confirmed Instagram connector list and history routes."""

	def __init__(self, client: ConnectorClient | None = None) -> None:
		if client is None:
			from meta_omnichannel.meta_connectors.factory import get_connector_client

			client = get_connector_client()
		self.client = client

	def list_conversations(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
		"""GET /api/instagram/conversations with envelope validation."""
		limit = max(1, min(int(limit), 100))
		offset = max(0, int(offset))
		response = self.client.request(
			"GET",
			CONVERSATIONS_PATH,
			params={"limit": limit, "offset": offset},
		)
		_raise_for_status(response.status_code, context="list_conversations")
		data = response.json() if response.content else {}
		return validate_conversations_list_envelope(data)

	def get_messages(
		self,
		*,
		account_id: str,
		sender_id: str,
		limit: int = 50,
		offset: int = 0,
	) -> dict[str, Any]:
		"""GET /api/instagram/conversations/{account_id}/{sender_id}/messages.

		Path segments are percent-encoded. Response envelope and message fields are
		validated fail-closed. Does not write to the database.
		"""
		account_id = (account_id or "").strip()
		sender_id = (sender_id or "").strip()
		if not account_id or not sender_id:
			raise ConnectorError("get_messages: account_id and sender_id are required")

		limit = max(1, min(int(limit), 100))
		offset = max(0, int(offset))
		path = (
			f"{CONVERSATIONS_PATH}/"
			f"{quote(account_id, safe='')}/"
			f"{quote(sender_id, safe='')}/"
			f"messages"
		)
		response = self.client.request(
			"GET",
			path,
			params={"limit": limit, "offset": offset},
		)
		_raise_for_status(response.status_code, context="get_messages")
		data = response.json() if response.content else {}
		return validate_messages_envelope(data)

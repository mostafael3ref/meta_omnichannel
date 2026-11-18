"""Instagram connector adapter — list conversations only (Step 1).

Confirmed endpoint: ``GET /api/instagram/conversations``.

History and outbound messaging remain disabled in Step 1.
"""

from __future__ import annotations

from typing import Any

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
	"""Typed client for confirmed Instagram connector list route."""

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

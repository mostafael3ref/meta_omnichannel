"""Facebook Messenger adapter for confirmed connector endpoints only."""

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

CONVERSATIONS_PATH = "/api/facebook/conversations"
SEND_PATH = "/api/facebook/messages/send"


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


class FacebookConnectorAdapter:
	"""Typed client for confirmed Facebook connector routes."""

	def __init__(self, client: ConnectorClient | None = None) -> None:
		if client is None:
			from meta_omnichannel.meta_connectors.factory import get_connector_client

			client = get_connector_client()
		self.client = client

	def list_conversations(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
		limit = max(1, min(int(limit), 100))
		offset = max(0, int(offset))
		response = self.client.request(
			"GET",
			CONVERSATIONS_PATH,
			params={"limit": limit, "offset": offset},
		)
		_raise_for_status(response.status_code, context="list_conversations")
		data = response.json() if response.content else {}
		if not isinstance(data, dict):
			raise ConnectorError("list_conversations: response must be a JSON object")
		return data

	def get_messages(
		self,
		*,
		page_id: str,
		sender_id: str,
		limit: int = 50,
		offset: int = 0,
	) -> dict[str, Any]:
		page_id = (page_id or "").strip()
		sender_id = (sender_id or "").strip()
		if not page_id or not sender_id:
			raise ConnectorError("get_messages: page_id and sender_id are required")
		limit = max(1, min(int(limit), 100))
		offset = max(0, int(offset))
		path = f"{CONVERSATIONS_PATH}/{page_id}/{sender_id}/messages"
		response = self.client.request("GET", path, params={"limit": limit, "offset": offset})
		_raise_for_status(response.status_code, context="get_messages")
		data = response.json() if response.content else {}
		if not isinstance(data, dict):
			raise ConnectorError("get_messages: response must be a JSON object")
		return data

	def send_text(
		self,
		*,
		page_id: str,
		recipient_id: str,
		text: str,
		idempotency_key: str,
	) -> dict[str, Any]:
		page_id = (page_id or "").strip()
		recipient_id = (recipient_id or "").strip()
		text = (text or "").strip()
		idempotency_key = (idempotency_key or "").strip()
		if not page_id or not recipient_id or not text or not idempotency_key:
			raise ConnectorError(
				"send_text: page_id, recipient_id, text, and idempotency_key are required"
			)

		response = self.client.request(
			"POST",
			SEND_PATH,
			json_body={
				"page_id": page_id,
				"recipient_id": recipient_id,
				"text": text,
				"idempotency_key": idempotency_key,
			},
		)
		_raise_for_status(response.status_code, context="send_text")
		data = response.json() if response.content else {}
		if data and not isinstance(data, dict):
			raise ConnectorError("send_text: response must be a JSON object")
		return data or {"ok": True}

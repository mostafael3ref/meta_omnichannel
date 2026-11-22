"""Facebook Messenger adapter for confirmed connector endpoints only."""

from __future__ import annotations

from typing import Any, NamedTuple
from urllib.parse import quote

from meta_omnichannel.meta_connectors.client import ConnectorClient
from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorRateLimitError,
	ConnectorServerError,
)

CONVERSATIONS_PATH = "/api/facebook/conversations"
MESSAGES_PATH = "/api/facebook/messages"
SEND_PATH = "/api/facebook/messages/send"
# Meta Messenger attachment upload limit; refuse larger Connector payloads.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class AttachmentPayload(NamedTuple):
	"""Binary attachment returned by the Connector (never logged)."""

	content: bytes
	content_type: str


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


def _parse_attachment_index(attachment_index: Any) -> int:
	"""Return a non-negative int index; reject bools and non-integers."""
	# bool is a subclass of int — reject explicitly.
	if isinstance(attachment_index, bool) or not isinstance(attachment_index, int):
		raise ConnectorError(
			"get_message_attachment: attachment_index must be a non-negative integer"
		)
	if attachment_index < 0:
		raise ConnectorError(
			"get_message_attachment: attachment_index must be a non-negative integer"
		)
	return attachment_index


def _content_type_from_headers(headers: Any) -> str:
	raw = ""
	if headers is not None:
		try:
			raw = str(headers.get("Content-Type") or headers.get("content-type") or "")
		except Exception:
			raw = ""
	return raw.split(";", 1)[0].strip().lower()


def _is_allowed_attachment_content_type(content_type: str) -> bool:
	"""Accept media MIME types; reject HTML/JSON and other non-attachment bodies."""
	if not content_type:
		return False
	if content_type in ("application/pdf", "application/octet-stream"):
		return True
	return content_type.startswith(("image/", "video/", "audio/"))


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

	def get_message_attachment(
		self,
		*,
		page_id: str,
		message_id: str,
		attachment_index: int = 0,
	) -> AttachmentPayload:
		"""Fetch attachment bytes from the Connector (not Facebook CDN).

		Returns ``AttachmentPayload(content, content_type)``. Never logs bytes or
		includes API keys / URLs in raised errors.
		"""
		page_id = (page_id or "").strip()
		message_id = (message_id or "").strip()
		if not page_id or not message_id:
			raise ConnectorError("get_message_attachment: page_id and message_id are required")
		index = _parse_attachment_index(attachment_index)

		# Encode every path segment so IDs cannot inject extra path components.
		path = (
			f"{MESSAGES_PATH}/"
			f"{quote(page_id, safe='')}/"
			f"{quote(message_id, safe='')}/"
			f"attachments/{index}"
		)
		response = self.client.request("GET", path)
		_raise_for_status(response.status_code, context="get_message_attachment")

		raw_content = getattr(response, "content", None)
		# requests.Response.content is bytes; also accept bytearray; reject all else.
		if isinstance(raw_content, bytes):
			content = raw_content
		elif isinstance(raw_content, bytearray):
			content = bytes(raw_content)
		else:
			raise ConnectorError("get_message_attachment: invalid response content")
		if not content:
			raise ConnectorError("get_message_attachment: empty response")
		if len(content) > MAX_ATTACHMENT_BYTES:
			raise ConnectorError("get_message_attachment: attachment exceeds size limit")

		content_type = _content_type_from_headers(getattr(response, "headers", None))
		if not _is_allowed_attachment_content_type(content_type):
			raise ConnectorError("get_message_attachment: unexpected content type")

		# Never call response.json() — body is binary media.
		return AttachmentPayload(content=content, content_type=content_type)

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

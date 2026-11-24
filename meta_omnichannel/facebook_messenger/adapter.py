"""Facebook Messenger adapter for confirmed connector endpoints only."""

from __future__ import annotations

import io
import zipfile
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
SEND_IMAGE_PATH = "/api/facebook/messages/send-image"
SEND_DOCUMENT_PATH = "/api/facebook/messages/send-document"
# Meta Messenger attachment upload limit; refuse larger Connector payloads (inbound GET).
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# Confirmed outbound image send contract (Connector → Meta Attachment Upload API).
MAX_OUTBOUND_IMAGE_BYTES = 8 * 1024 * 1024
# Confirmed outbound document send contract (Connector send-document).
MAX_OUTBOUND_DOCUMENT_BYTES = 8 * 1024 * 1024
_OUTBOUND_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif"})
_OUTBOUND_IMAGE_TYPE_ALIASES = {"image/jpg": "image/jpeg"}
_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_OUTBOUND_DOCUMENT_TYPES = frozenset({_PDF_MIME, _DOCX_MIME})
_OUTBOUND_DOCUMENT_DEFAULT_NAME = {
	_PDF_MIME: "document.pdf",
	_DOCX_MIME: "document.docx",
}


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


def _normalize_outbound_image_content_type(content_type: str) -> str:
	ct = (content_type or "").split(";", 1)[0].strip().lower()
	return _OUTBOUND_IMAGE_TYPE_ALIASES.get(ct, ct)


def _validate_outbound_image(
	*,
	image_bytes: bytes,
	filename: str,
	content_type: str,
) -> tuple[bytes, str, str]:
	"""Return ``(bytes, filename, content_type)`` or raise before any Connector call."""
	if not isinstance(image_bytes, (bytes, bytearray)):
		raise ConnectorError("send_image: image_bytes must be bytes")
	payload = bytes(image_bytes)
	if not payload:
		raise ConnectorError("send_image: image_bytes must not be empty")
	if len(payload) > MAX_OUTBOUND_IMAGE_BYTES:
		raise ConnectorError("send_image: image exceeds 8 MiB limit")

	ct = _normalize_outbound_image_content_type(content_type)
	if ct not in _OUTBOUND_IMAGE_TYPES:
		raise ConnectorError("send_image: content_type must be image/jpeg, image/png, or image/gif")

	name = (filename or "").strip() or f"image.{ct.split('/', 1)[1]}"
	# Path separators must not leak into the multipart filename.
	name = name.replace("\\", "_").replace("/", "_")
	return payload, name, ct


def _normalize_outbound_document_content_type(content_type: str) -> str:
	return (content_type or "").split(";", 1)[0].strip().lower()


def _looks_like_pdf(payload: bytes) -> bool:
	"""PDF files must start with the ``%PDF-`` signature."""
	return payload.startswith(b"%PDF-")


def _looks_like_docx(payload: bytes) -> bool:
	"""DOCX is OOXML: ZIP container with ``[Content_Types].xml`` and a Word part."""
	if len(payload) < 4 or not payload.startswith(b"PK\x03\x04"):
		return False
	try:
		with zipfile.ZipFile(io.BytesIO(payload)) as zf:
			names = set(zf.namelist())
			if "[Content_Types].xml" not in names:
				return False
			if "word/document.xml" in names:
				return True
			try:
				content_types = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
			except Exception:
				return False
			return "wordprocessingml" in content_types.lower()
	except zipfile.BadZipFile:
		return False


def _detect_outbound_document_mime(payload: bytes) -> str | None:
	"""Sniff PDF signature or DOCX OOXML structure (ignore declared extension)."""
	if not payload:
		return None
	if _looks_like_pdf(payload):
		return _PDF_MIME
	if _looks_like_docx(payload):
		return _DOCX_MIME
	return None


def _validate_outbound_document(
	*,
	document_bytes: bytes,
	filename: str,
	content_type: str,
) -> tuple[bytes, str, str]:
	"""Return ``(bytes, filename, content_type)`` or raise before any Connector call."""
	if not isinstance(document_bytes, (bytes, bytearray)):
		raise ConnectorError("send_document: document_bytes must be bytes")
	payload = bytes(document_bytes)
	if not payload:
		raise ConnectorError("send_document: document_bytes must not be empty")
	if len(payload) > MAX_OUTBOUND_DOCUMENT_BYTES:
		raise ConnectorError("send_document: document exceeds 8 MiB limit")

	declared = _normalize_outbound_document_content_type(content_type)
	if declared not in _OUTBOUND_DOCUMENT_TYPES:
		raise ConnectorError(
			"send_document: content_type must be application/pdf or "
			"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
		)

	detected = _detect_outbound_document_mime(payload)
	if not detected:
		raise ConnectorError("send_document: document must be a valid PDF or DOCX file")
	if declared != detected:
		raise ConnectorError(
			"send_document: document content does not match the declared MIME type"
		)

	name = (filename or "").strip() or _OUTBOUND_DOCUMENT_DEFAULT_NAME[detected]
	# Path separators must not leak into the multipart filename.
	name = name.replace("\\", "_").replace("/", "_")
	return payload, name, detected


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

	def send_image(
		self,
		*,
		page_id: str,
		recipient_id: str,
		idempotency_key: str,
		image_bytes: bytes,
		filename: str,
		content_type: str,
	) -> dict[str, Any]:
		"""POST multipart image bytes to the Connector (never a File URL).

		Does not retry. Caller maps timeouts / HTTP 5xx to Unknown delivery.
		"""
		page_id = (page_id or "").strip()
		recipient_id = (recipient_id or "").strip()
		idempotency_key = (idempotency_key or "").strip()
		if not page_id or not recipient_id or not idempotency_key:
			raise ConnectorError(
				"send_image: page_id, recipient_id, and idempotency_key are required"
			)

		payload, safe_name, mime = _validate_outbound_image(
			image_bytes=image_bytes,
			filename=filename,
			content_type=content_type,
		)

		response = self.client.request(
			"POST",
			SEND_IMAGE_PATH,
			data={
				"page_id": page_id,
				"recipient_id": recipient_id,
				"idempotency_key": idempotency_key,
			},
			files={
				"image": (safe_name, payload, mime),
			},
		)
		_raise_for_status(response.status_code, context="send_image")
		data = response.json() if response.content else {}
		if data and not isinstance(data, dict):
			raise ConnectorError("send_image: response must be a JSON object")
		return data or {"status": "sent"}

	def send_document(
		self,
		*,
		page_id: str,
		recipient_id: str,
		idempotency_key: str,
		document_bytes: bytes,
		filename: str,
		content_type: str,
	) -> dict[str, Any]:
		"""POST multipart PDF/DOCX bytes to the Connector (never a File URL).

		Does not retry. Caller maps timeouts / HTTP 5xx to Unknown delivery.
		"""
		page_id = (page_id or "").strip()
		recipient_id = (recipient_id or "").strip()
		idempotency_key = (idempotency_key or "").strip()
		if not page_id or not recipient_id or not idempotency_key:
			raise ConnectorError(
				"send_document: page_id, recipient_id, and idempotency_key are required"
			)

		payload, safe_name, mime = _validate_outbound_document(
			document_bytes=document_bytes,
			filename=filename,
			content_type=content_type,
		)

		response = self.client.request(
			"POST",
			SEND_DOCUMENT_PATH,
			data={
				"page_id": page_id,
				"recipient_id": recipient_id,
				"idempotency_key": idempotency_key,
			},
			files={
				"document": (safe_name, payload, mime),
			},
		)
		_raise_for_status(response.status_code, context="send_document")
		data = response.json() if response.content else {}
		if data and not isinstance(data, dict):
			raise ConnectorError("send_document: response must be a JSON object")
		return data or {"status": "sent"}

"""Facebook adapter tests with mocked ConnectorClient (no production calls)."""

from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from unittest.mock import MagicMock
from urllib.parse import quote

from meta_omnichannel.facebook_messenger.adapter import (
	MAX_ATTACHMENT_BYTES,
	MAX_OUTBOUND_DOCUMENT_BYTES,
	MAX_OUTBOUND_IMAGE_BYTES,
	SEND_DOCUMENT_PATH,
	SEND_IMAGE_PATH,
	FacebookConnectorAdapter,
	_DOCX_MIME,
	_PDF_MIME,
)
from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorRateLimitError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.utils.idempotency import (
	build_outbound_document_idempotency_key,
	build_outbound_idempotency_key,
	build_outbound_image_idempotency_key,
	outbound_document_nonce_key_prefix,
)


def _minimal_pdf() -> bytes:
	return (
		b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
		b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
		b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
	)


def _minimal_docx() -> bytes:
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as zf:
		zf.writestr(
			"[Content_Types].xml",
			'<?xml version="1.0" encoding="UTF-8"?>'
			'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
			'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
			'<Default Extension="xml" ContentType="application/xml"/>'
			'<Override PartName="/word/document.xml" '
			'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
			"</Types>",
		)
		zf.writestr(
			"_rels/.rels",
			'<?xml version="1.0" encoding="UTF-8"?>'
			'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
			'<Relationship Id="rId1" '
			'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
			'Target="word/document.xml"/>'
			"</Relationships>",
		)
		zf.writestr(
			"word/document.xml",
			'<?xml version="1.0" encoding="UTF-8"?>'
			'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
			"<w:body><w:p/></w:body></w:document>",
		)
	return buf.getvalue()


class TestIdempotency(unittest.TestCase):
	def test_stable_key(self):
		a = build_outbound_idempotency_key(
			conversation_name="CONV-1", text="hello", client_nonce="abc"
		)
		b = build_outbound_idempotency_key(
			conversation_name="CONV-1", text="hello", client_nonce="abc"
		)
		c = build_outbound_idempotency_key(
			conversation_name="CONV-1", text="hello!", client_nonce="abc"
		)
		self.assertEqual(a, b)
		self.assertNotEqual(a, c)


class TestFacebookAdapter(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = FacebookConnectorAdapter(client=self.client)

	def _resp(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r

	def _binary_resp(self, status, content: bytes, content_type: str):
		r = MagicMock()
		r.status_code = status
		r.content = content
		r.headers = {"Content-Type": content_type}
		r.json.side_effect = AssertionError("must not parse attachment as JSON")
		return r

	def test_list_conversations(self):
		self.client.request.return_value = self._resp(
			200, {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
		)
		data = self.adapter.list_conversations(limit=50, offset=0)
		self.assertEqual(data["total"], 0)
		kwargs = self.client.request.call_args
		self.assertEqual(kwargs.args[0], "GET")
		self.assertEqual(kwargs.args[1], "/api/facebook/conversations")

	def test_get_messages_path(self):
		self.client.request.return_value = self._resp(200, {"messages": []})
		self.adapter.get_messages(page_id="P1", sender_id="S1", limit=20, offset=0)
		path = self.client.request.call_args.args[1]
		self.assertEqual(path, "/api/facebook/conversations/P1/S1/messages")

	def test_send_text_posts_json(self):
		self.client.request.return_value = self._resp(200, {"ok": True})
		self.adapter.send_text(
			page_id="P1", recipient_id="S1", text="Hi", idempotency_key="k1"
		)
		kwargs = self.client.request.call_args.kwargs
		self.assertEqual(self.client.request.call_args.args[0], "POST")
		self.assertEqual(
			kwargs["json_body"]["idempotency_key"],
			"k1",
		)

	def test_auth_error(self):
		self.client.request.return_value = self._resp(401, {})
		with self.assertRaises(ConnectorAuthError):
			self.adapter.list_conversations()

	def test_rate_limit(self):
		self.client.request.return_value = self._resp(429, {})
		with self.assertRaises(ConnectorRateLimitError):
			self.adapter.list_conversations()


class TestGetMessageAttachment(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = FacebookConnectorAdapter(client=self.client)

	def _binary_resp(self, status, content: bytes, content_type: str):
		r = MagicMock()
		r.status_code = status
		r.content = content
		r.headers = {"Content-Type": content_type}
		r.json.side_effect = AssertionError("must not parse attachment as JSON")
		return r

	def test_successful_image(self):
		body = b"\xff\xd8\xffJPEGDATA"
		self.client.request.return_value = self._binary_resp(200, body, "image/jpeg")
		payload = self.adapter.get_message_attachment(
			page_id="P1", message_id="m_abc", attachment_index=0
		)
		self.assertEqual(payload.content, body)
		self.assertIsInstance(payload.content, bytes)
		self.assertEqual(payload.content_type, "image/jpeg")
		self.assertEqual(self.client.request.call_args.args[0], "GET")
		self.assertEqual(
			self.client.request.call_args.args[1],
			"/api/facebook/messages/P1/m_abc/attachments/0",
		)

	def test_response_content_bytes_returned_exactly(self):
		"""Regression: requests.Response.content is bytes and must be accepted as-is."""
		body = b"\x89PNG\r\n\x1a\nexact-bytes"
		self.client.request.return_value = self._binary_resp(200, body, "image/png")
		payload = self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIsInstance(payload.content, bytes)
		self.assertIs(payload.content, body)
		self.assertEqual(payload.content, body)

	def test_response_content_bytearray_converted_to_bytes(self):
		raw = bytearray(b"audio-bytes-here")
		resp = self._binary_resp(200, b"", "audio/mpeg")
		resp.content = raw
		self.client.request.return_value = resp
		payload = self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIsInstance(payload.content, bytes)
		self.assertNotIsInstance(payload.content, bytearray)
		self.assertEqual(payload.content, b"audio-bytes-here")

	def test_invalid_response_content_type_rejected(self):
		for bad in (None, "not-bytes", 123, object()):
			resp = self._binary_resp(200, b"x", "image/jpeg")
			resp.content = bad
			self.client.request.return_value = resp
			with self.assertRaises(ConnectorError) as ctx:
				self.adapter.get_message_attachment(page_id="P1", message_id="m1")
			self.assertIn("invalid response content", str(ctx.exception).lower())

	def test_successful_video_and_pdf(self):
		video = b"\x00\x00\x00\x18ftypmp42"
		self.client.request.return_value = self._binary_resp(200, video, "video/mp4")
		p = self.adapter.get_message_attachment(page_id="P1", message_id="m1", attachment_index=1)
		self.assertEqual(p.content_type, "video/mp4")
		self.assertEqual(p.content, video)

		pdf = b"%PDF-1.4 mock"
		self.client.request.return_value = self._binary_resp(200, pdf, "application/pdf")
		p2 = self.adapter.get_message_attachment(page_id="P1", message_id="m1", attachment_index=2)
		self.assertEqual(p2.content_type, "application/pdf")
		self.assertEqual(p2.content, pdf)

	def test_path_encoding_prevents_injection(self):
		page = "P1/../evil"
		mid = "m id/with?q=1"
		self.client.request.return_value = self._binary_resp(200, b"abc", "image/png")
		self.adapter.get_message_attachment(page_id=page, message_id=mid, attachment_index=0)
		path = self.client.request.call_args.args[1]
		self.assertEqual(
			path,
			f"/api/facebook/messages/{quote(page, safe='')}/{quote(mid, safe='')}/attachments/0",
		)
		self.assertNotIn("../", path)
		self.assertNotIn(" ", path)

	def test_missing_ids(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.get_message_attachment(page_id="", message_id="m1")
		self.assertIn("page_id and message_id", str(ctx.exception))
		self.client.request.assert_not_called()

		with self.assertRaises(ConnectorError):
			self.adapter.get_message_attachment(page_id="P1", message_id="  ")
		self.client.request.assert_not_called()

	def test_invalid_indices(self):
		for bad in (-1, True, False, 1.5, "0", None):
			with self.assertRaises(ConnectorError):
				self.adapter.get_message_attachment(
					page_id="P1", message_id="m1", attachment_index=bad  # type: ignore[arg-type]
				)
		self.client.request.assert_not_called()

	def test_status_mapping(self):
		cases = [
			(401, ConnectorAuthError),
			(403, ConnectorForbiddenError),
			(404, ConnectorError),
			(429, ConnectorRateLimitError),
			(500, ConnectorServerError),
			(503, ConnectorServerError),
		]
		for status, exc_type in cases:
			self.client.request.return_value = self._binary_resp(
				status, b"x", "image/jpeg"
			)
			with self.assertRaises(exc_type) as ctx:
				self.adapter.get_message_attachment(page_id="P1", message_id="m1")
			msg = str(ctx.exception)
			self.assertNotIn("api_key", msg.lower())
			self.assertNotIn("http://", msg.lower())
			self.assertNotIn("https://", msg.lower())

	def test_empty_body_rejected(self):
		self.client.request.return_value = self._binary_resp(200, b"", "image/jpeg")
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIn("empty", str(ctx.exception).lower())

	def test_html_and_json_rejected(self):
		self.client.request.return_value = self._binary_resp(
			200, b"<html>nope</html>", "text/html"
		)
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIn("content type", str(ctx.exception).lower())

		self.client.request.return_value = self._binary_resp(
			200, b'{"error":true}', "application/json"
		)
		with self.assertRaises(ConnectorError) as ctx2:
			self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIn("content type", str(ctx2.exception).lower())

	def test_oversized_rejected(self):
		huge = b"x" * (MAX_ATTACHMENT_BYTES + 1)
		self.client.request.return_value = self._binary_resp(200, huge, "image/jpeg")
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.get_message_attachment(page_id="P1", message_id="m1")
		self.assertIn("size", str(ctx.exception).lower())

	def test_existing_methods_unchanged_after_attachment_addition(self):
		"""get_messages / send_text paths and JSON handling stay as before."""
		self.client.request.return_value = self._resp_json(200, {"messages": []})
		self.adapter.get_messages(page_id="P1", sender_id="S1")
		self.assertEqual(
			self.client.request.call_args.args[1],
			"/api/facebook/conversations/P1/S1/messages",
		)

		self.client.request.return_value = self._resp_json(200, {"ok": True})
		out = self.adapter.send_text(
			page_id="P1", recipient_id="S1", text="Hi", idempotency_key="k1"
		)
		self.assertEqual(out, {"ok": True})
		self.assertEqual(self.client.request.call_args.args[0], "POST")
		self.assertEqual(self.client.request.call_args.args[1], "/api/facebook/messages/send")

	def _resp_json(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r


class TestSendImage(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = FacebookConnectorAdapter(client=self.client)
		self.png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

	def _resp(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r

	def test_send_image_multipart_contract(self):
		self.client.request.return_value = self._resp(
			200,
			{"status": "sent", "page_id": "P1", "message_id": "m_1", "duplicate": False},
		)
		out = self.adapter.send_image(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="idem-1",
			image_bytes=self.png,
			filename="photo.png",
			content_type="image/png",
		)
		self.assertEqual(out["status"], "sent")
		self.assertEqual(out["message_id"], "m_1")
		args, kwargs = self.client.request.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], SEND_IMAGE_PATH)
		self.assertEqual(SEND_IMAGE_PATH, "/api/facebook/messages/send-image")
		self.assertIsNone(kwargs.get("json_body"))
		self.assertEqual(
			kwargs["data"],
			{"page_id": "P1", "recipient_id": "S1", "idempotency_key": "idem-1"},
		)
		name, body, mime = kwargs["files"]["image"]
		self.assertEqual(name, "photo.png")
		self.assertEqual(body, self.png)
		self.assertEqual(mime, "image/png")
		self.assertEqual(self.client.request.call_count, 1)

	def test_send_text_regression_still_json(self):
		self.client.request.return_value = self._resp(200, {"ok": True})
		self.adapter.send_text(
			page_id="P1", recipient_id="S1", text="Hi", idempotency_key="k1"
		)
		kwargs = self.client.request.call_args.kwargs
		self.assertEqual(self.client.request.call_args.args[1], "/api/facebook/messages/send")
		self.assertIn("json_body", kwargs)
		self.assertNotIn("files", kwargs)

	def test_rejects_unsupported_mime_before_request(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="x.webp",
				content_type="image/webp",
			)
		self.assertIn("content_type", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_oversized_image_before_request(self):
		huge = b"\x00" * (MAX_OUTBOUND_IMAGE_BYTES + 1)
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=huge,
				filename="big.jpg",
				content_type="image/jpeg",
			)
		self.assertIn("8 mib", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_image_jpg_alias_normalized(self):
		self.client.request.return_value = self._resp(
			200, {"status": "sent", "message_id": "m", "duplicate": False}
		)
		self.adapter.send_image(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="k",
			image_bytes=self.png,
			filename="a.jpg",
			content_type="image/jpg",
		)
		_name, _body, mime = self.client.request.call_args.kwargs["files"]["image"]
		self.assertEqual(mime, "image/jpeg")

	def test_http_400_is_definitive_error(self):
		self.client.request.return_value = self._resp(400, {"detail": "invalid"})
		with self.assertRaises(ConnectorError):
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="a.png",
				content_type="image/png",
			)

	def test_http_403_is_forbidden(self):
		self.client.request.return_value = self._resp(403, {})
		with self.assertRaises(ConnectorForbiddenError):
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="a.png",
				content_type="image/png",
			)

	def test_http_409_is_definitive_error(self):
		self.client.request.return_value = self._resp(409, {"detail": "conflict"})
		with self.assertRaises(ConnectorError):
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="a.png",
				content_type="image/png",
			)

	def test_http_502_is_server_error_for_unknown_mapping(self):
		self.client.request.return_value = self._resp(502, {})
		with self.assertRaises(ConnectorServerError):
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="a.png",
				content_type="image/png",
			)

	def test_timeout_unreachable_no_retry(self):
		self.client.request.side_effect = ConnectorUnreachableError(
			"Connector request timed out"
		)
		with self.assertRaises(ConnectorUnreachableError):
			self.adapter.send_image(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				image_bytes=self.png,
				filename="a.png",
				content_type="image/png",
			)
		self.assertEqual(self.client.request.call_count, 1)

	def test_duplicate_true_response_returned(self):
		self.client.request.return_value = self._resp(
			200,
			{"status": "sent", "message_id": "m_dup", "duplicate": True},
		)
		out = self.adapter.send_image(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="k",
			image_bytes=self.png,
			filename="a.png",
			content_type="image/png",
		)
		self.assertTrue(out["duplicate"])
		self.assertEqual(out["message_id"], "m_dup")


class TestOutboundDocumentIdempotencyHelpers(unittest.TestCase):
	def test_stable_document_key_is_nonce_scoped(self):
		sha = hashlib.sha256(_minimal_pdf()).hexdigest()
		a = build_outbound_document_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha
		)
		b = build_outbound_document_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha
		)
		self.assertEqual(a, b)
		self.assertTrue(a.startswith("oc-doc:"))
		self.assertEqual(
			a,
			outbound_document_nonce_key_prefix(
				conversation_name="CONV-1", client_nonce="n1"
			),
		)

	def test_same_nonce_same_key_regardless_of_document_hash(self):
		sha1 = hashlib.sha256(_minimal_pdf()).hexdigest()
		sha2 = hashlib.sha256(_minimal_docx()).hexdigest()
		a = build_outbound_document_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha1
		)
		b = build_outbound_document_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha2
		)
		self.assertEqual(a, b)
		self.assertNotEqual(sha1, sha2)

	def test_document_and_image_namespaces_are_separate(self):
		doc = build_outbound_document_idempotency_key(
			conversation_name="CONV-1", client_nonce="same-nonce"
		)
		img = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="same-nonce"
		)
		self.assertTrue(doc.startswith("oc-doc:"))
		self.assertTrue(img.startswith("oc-img:"))
		self.assertNotEqual(doc, img)
		self.assertFalse(doc.startswith("oc-img:"))
		self.assertFalse(img.startswith("oc-doc:"))

	def test_image_key_algorithm_unchanged(self):
		a = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1"
		)
		self.assertEqual(a, "oc-img:CONV-1:n1")


class TestSendDocument(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = FacebookConnectorAdapter(client=self.client)
		self.pdf = _minimal_pdf()
		self.docx = _minimal_docx()

	def _resp(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r

	def test_send_pdf_multipart_contract(self):
		self.client.request.return_value = self._resp(
			200,
			{"status": "sent", "page_id": "P1", "message_id": "m_pdf", "duplicate": False},
		)
		out = self.adapter.send_document(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="idem-doc-1",
			document_bytes=self.pdf,
			filename="report.pdf",
			content_type=_PDF_MIME,
		)
		self.assertEqual(out["status"], "sent")
		self.assertEqual(out["message_id"], "m_pdf")
		args, kwargs = self.client.request.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], SEND_DOCUMENT_PATH)
		self.assertEqual(SEND_DOCUMENT_PATH, "/api/facebook/messages/send-document")
		self.assertIsNone(kwargs.get("json_body"))
		self.assertEqual(
			kwargs["data"],
			{"page_id": "P1", "recipient_id": "S1", "idempotency_key": "idem-doc-1"},
		)
		name, body, mime = kwargs["files"]["document"]
		self.assertEqual(name, "report.pdf")
		self.assertEqual(body, self.pdf)
		self.assertEqual(mime, _PDF_MIME)
		self.assertEqual(self.client.request.call_count, 1)

	def test_send_docx_multipart_contract(self):
		self.client.request.return_value = self._resp(
			200, {"status": "sent", "message_id": "m_docx", "duplicate": False}
		)
		out = self.adapter.send_document(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="idem-doc-2",
			document_bytes=self.docx,
			filename="notes.docx",
			content_type=_DOCX_MIME,
		)
		self.assertEqual(out["message_id"], "m_docx")
		name, body, mime = self.client.request.call_args.kwargs["files"]["document"]
		self.assertEqual(name, "notes.docx")
		self.assertEqual(body, self.docx)
		self.assertEqual(mime, _DOCX_MIME)

	def test_sanitizes_path_separators_in_filename(self):
		self.client.request.return_value = self._resp(
			200, {"status": "sent", "message_id": "m", "duplicate": False}
		)
		self.adapter.send_document(
			page_id="P1",
			recipient_id="S1",
			idempotency_key="k",
			document_bytes=self.pdf,
			filename=r"..\..\evil/report.pdf",
			content_type=_PDF_MIME,
		)
		name, _body, _mime = self.client.request.call_args.kwargs["files"]["document"]
		self.assertEqual(name, ".._.._evil_report.pdf")
		self.assertNotIn("/", name)
		self.assertNotIn("\\", name)

	def test_rejects_empty_document_before_request(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=b"",
				filename="empty.pdf",
				content_type=_PDF_MIME,
			)
		self.assertIn("empty", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_oversized_document_before_request(self):
		huge = b"%PDF-" + b"\x00" * MAX_OUTBOUND_DOCUMENT_BYTES
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=huge,
				filename="big.pdf",
				content_type=_PDF_MIME,
			)
		self.assertIn("8 mib", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_unsupported_mime_before_request(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.pdf,
				filename="x.txt",
				content_type="text/plain",
			)
		self.assertIn("content_type", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_mismatched_pdf_declaration(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.docx,
				filename="lie.pdf",
				content_type=_PDF_MIME,
			)
		self.assertIn("does not match", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_mismatched_docx_declaration(self):
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.pdf,
				filename="lie.docx",
				content_type=_DOCX_MIME,
			)
		self.assertIn("does not match", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_random_zip_as_docx(self):
		buf = io.BytesIO()
		with zipfile.ZipFile(buf, "w") as zf:
			zf.writestr("readme.txt", "not a word document")
		with self.assertRaises(ConnectorError) as ctx:
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=buf.getvalue(),
				filename="fake.docx",
				content_type=_DOCX_MIME,
			)
		self.assertIn("valid pdf or docx", str(ctx.exception).lower())
		self.client.request.assert_not_called()

	def test_rejects_non_pdf_signature(self):
		with self.assertRaises(ConnectorError):
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=b"not-a-pdf",
				filename="x.pdf",
				content_type=_PDF_MIME,
			)
		self.client.request.assert_not_called()

	def test_http_502_is_server_error_for_unknown_mapping(self):
		self.client.request.return_value = self._resp(502, {})
		with self.assertRaises(ConnectorServerError):
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.pdf,
				filename="a.pdf",
				content_type=_PDF_MIME,
			)

	def test_timeout_unreachable_no_retry(self):
		self.client.request.side_effect = ConnectorUnreachableError(
			"Connector request timed out"
		)
		with self.assertRaises(ConnectorUnreachableError):
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.pdf,
				filename="a.pdf",
				content_type=_PDF_MIME,
			)
		self.assertEqual(self.client.request.call_count, 1)

	def test_http_403_is_forbidden(self):
		self.client.request.return_value = self._resp(403, {})
		with self.assertRaises(ConnectorForbiddenError):
			self.adapter.send_document(
				page_id="P1",
				recipient_id="S1",
				idempotency_key="k",
				document_bytes=self.pdf,
				filename="a.pdf",
				content_type=_PDF_MIME,
			)


if __name__ == "__main__":
	unittest.main()

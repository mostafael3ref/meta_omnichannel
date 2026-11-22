"""Facebook adapter tests with mocked ConnectorClient (no production calls)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from urllib.parse import quote

from meta_omnichannel.facebook_messenger.adapter import (
	MAX_ATTACHMENT_BYTES,
	FacebookConnectorAdapter,
)
from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorRateLimitError,
	ConnectorServerError,
)
from meta_omnichannel.utils.idempotency import build_outbound_idempotency_key


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


if __name__ == "__main__":
	unittest.main()

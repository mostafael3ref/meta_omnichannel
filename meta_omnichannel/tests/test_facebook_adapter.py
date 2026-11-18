"""Facebook adapter tests with mocked ConnectorClient (no production calls)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from meta_omnichannel.facebook_messenger.adapter import FacebookConnectorAdapter
from meta_omnichannel.meta_connectors.errors import ConnectorAuthError, ConnectorRateLimitError
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


if __name__ == "__main__":
	unittest.main()

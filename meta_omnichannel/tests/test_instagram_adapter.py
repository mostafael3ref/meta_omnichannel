"""Instagram list + message history adapter tests (mocked HTTP — no production connector)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from urllib.parse import quote

from meta_omnichannel.instagram.adapter import (
	CONVERSATIONS_PATH,
	InstagramConnectorAdapter,
	conversation_items_ready_for_sync,
	validate_conversations_list_envelope,
	validate_messages_envelope,
)
from meta_omnichannel.meta_connectors.errors import ConnectorAuthError, ConnectorError
from meta_omnichannel.utils.threading_keys import instagram_thread_key


def _envelope(**overrides):
	payload = {
		"tenant_id": "tenant-1",
		"provider": "instagram",
		"total": 0,
		"conversations": [],
		"limit": 50,
		"offset": 0,
		"has_more": False,
		"next_offset": None,
	}
	payload.update(overrides)
	return payload


def _messages_envelope(**overrides):
	payload = {
		"tenant_id": "tenant-1",
		"provider": "instagram",
		"account_id": "IG_BIZ",
		"sender_id": "IGSID_1",
		"total": 0,
		"messages": [],
		"limit": 50,
		"offset": 0,
		"has_more": False,
		"next_offset": None,
	}
	payload.update(overrides)
	return payload


def _message(**overrides):
	row = {
		"direction": "inbound",
		"text": "hello",
		"attachments": [],
		"timestamp": "2024-01-01T00:00:00Z",
		"message_id": "mid_1",
	}
	row.update(overrides)
	return row


class TestEnvelopeValidation(unittest.TestCase):
	def test_accepts_confirmed_envelope(self):
		payload = validate_conversations_list_envelope(_envelope(total=1, conversations=[{"account_id": "a"}]))
		self.assertEqual(payload["provider"], "instagram")

	def test_rejects_missing_envelope_keys(self):
		with self.assertRaises(ConnectorError) as ctx:
			validate_conversations_list_envelope({"conversations": []})
		self.assertIn("missing envelope keys", str(ctx.exception))

	def test_rejects_non_list_conversations(self):
		with self.assertRaises(ConnectorError):
			validate_conversations_list_envelope(_envelope(conversations="bad"))

	def test_rejects_non_object_items(self):
		with self.assertRaises(ConnectorError):
			validate_conversations_list_envelope(_envelope(conversations=["x"]))


class TestSyncReadiness(unittest.TestCase):
	def test_empty_list_is_ready(self):
		ready, missing = conversation_items_ready_for_sync(_envelope())
		self.assertTrue(ready)
		self.assertEqual(missing, [])

	def test_missing_identity_fields_blocked(self):
		ready, missing = conversation_items_ready_for_sync(
			_envelope(conversations=[{"foo": "bar"}])
		)
		self.assertFalse(ready)
		self.assertIn("account_id", missing)
		self.assertIn("sender_id", missing)

	def test_ready_when_account_and_sender_present(self):
		ready, missing = conversation_items_ready_for_sync(
			_envelope(
				conversations=[
					{"account_id": "IG_BIZ", "sender_id": "IGSID_1"},
					{"account_id": "IG_BIZ", "sender_id": "IGSID_2"},
				]
			)
		)
		self.assertTrue(ready)
		self.assertEqual(missing, [])


class TestInstagramAdapter(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = InstagramConnectorAdapter(client=self.client)

	def _resp(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r

	def test_list_path_and_pagination_params(self):
		self.client.request.return_value = self._resp(200, _envelope(total=2))
		data = self.adapter.list_conversations(limit=25, offset=10)
		self.assertEqual(data["total"], 2)
		args, kwargs = self.client.request.call_args
		self.assertEqual(args[0], "GET")
		self.assertEqual(args[1], CONVERSATIONS_PATH)
		self.assertEqual(kwargs["params"], {"limit": 25, "offset": 10})

	def test_clamps_limit_and_offset(self):
		self.client.request.return_value = self._resp(200, _envelope())
		self.adapter.list_conversations(limit=999, offset=-5)
		params = self.client.request.call_args.kwargs["params"]
		self.assertEqual(params["limit"], 100)
		self.assertEqual(params["offset"], 0)

	def test_auth_error(self):
		self.client.request.return_value = self._resp(401, {})
		with self.assertRaises(ConnectorAuthError):
			self.adapter.list_conversations()

	def test_invalid_envelope_raises(self):
		self.client.request.return_value = self._resp(200, {"ok": True})
		with self.assertRaises(ConnectorError):
			self.adapter.list_conversations()

	def test_thread_key(self):
		self.assertEqual(instagram_thread_key("A", "S"), "A:S")


class TestUpsertMappingPure(unittest.TestCase):
	"""Mapping helpers that do not require Frappe are covered via readiness checks."""

	def test_aliases_are_not_accepted_for_readiness(self):
		# Do not treat Facebook-style page_id/psid as Instagram identity.
		ready, missing = conversation_items_ready_for_sync(
			_envelope(conversations=[{"page_id": "P", "psid": "S", "senderId": "S"}])
		)
		self.assertFalse(ready)
		self.assertEqual(missing, ["account_id", "sender_id"])


class TestMessagesEnvelopeValidation(unittest.TestCase):
	def test_accepts_confirmed_envelope_and_messages(self):
		payload = validate_messages_envelope(
			_messages_envelope(total=1, messages=[_message()])
		)
		self.assertEqual(payload["account_id"], "IG_BIZ")
		self.assertEqual(len(payload["messages"]), 1)

	def test_accepts_nullable_message_id_and_text(self):
		payload = validate_messages_envelope(
			_messages_envelope(
				messages=[_message(message_id=None, text=None)],
			)
		)
		self.assertIsNone(payload["messages"][0]["message_id"])
		self.assertIsNone(payload["messages"][0]["text"])

	def test_rejects_missing_envelope_keys(self):
		with self.assertRaises(ConnectorError) as ctx:
			validate_messages_envelope({"messages": []})
		self.assertIn("missing envelope keys", str(ctx.exception))

	def test_rejects_non_list_messages(self):
		with self.assertRaises(ConnectorError):
			validate_messages_envelope(_messages_envelope(messages="bad"))

	def test_rejects_non_object_items(self):
		with self.assertRaises(ConnectorError):
			validate_messages_envelope(_messages_envelope(messages=["x"]))

	def test_rejects_missing_message_fields(self):
		with self.assertRaises(ConnectorError) as ctx:
			validate_messages_envelope(
				_messages_envelope(messages=[{"direction": "inbound"}])
			)
		self.assertIn("missing fields", str(ctx.exception))

	def test_rejects_non_string_message_id(self):
		with self.assertRaises(ConnectorError):
			validate_messages_envelope(
				_messages_envelope(messages=[_message(message_id=123)])
			)

	def test_rejects_non_list_attachments(self):
		with self.assertRaises(ConnectorError):
			validate_messages_envelope(
				_messages_envelope(messages=[_message(attachments=None)])
			)


class TestInstagramGetMessages(unittest.TestCase):
	def setUp(self):
		self.client = MagicMock()
		self.adapter = InstagramConnectorAdapter(client=self.client)

	def _resp(self, status, payload):
		r = MagicMock()
		r.status_code = status
		r.content = b"{}"
		r.json.return_value = payload
		return r

	def test_path_encoding_and_pagination_params(self):
		account_id = "IG/BIZ"
		sender_id = "IGSID 1"
		self.client.request.return_value = self._resp(
			200,
			_messages_envelope(account_id=account_id, sender_id=sender_id, total=1),
		)
		data = self.adapter.get_messages(
			account_id=account_id,
			sender_id=sender_id,
			limit=20,
			offset=5,
		)
		self.assertEqual(data["total"], 1)
		args, kwargs = self.client.request.call_args
		self.assertEqual(args[0], "GET")
		expected_path = (
			f"{CONVERSATIONS_PATH}/"
			f"{quote(account_id, safe='')}/"
			f"{quote(sender_id, safe='')}/"
			f"messages"
		)
		self.assertEqual(args[1], expected_path)
		self.assertEqual(kwargs["params"], {"limit": 20, "offset": 5})

	def test_clamps_limit_and_offset(self):
		self.client.request.return_value = self._resp(200, _messages_envelope())
		self.adapter.get_messages(account_id="A", sender_id="S", limit=999, offset=-3)
		params = self.client.request.call_args.kwargs["params"]
		self.assertEqual(params["limit"], 100)
		self.assertEqual(params["offset"], 0)

	def test_requires_account_and_sender(self):
		with self.assertRaises(ConnectorError):
			self.adapter.get_messages(account_id="", sender_id="S")
		with self.assertRaises(ConnectorError):
			self.adapter.get_messages(account_id="A", sender_id="  ")
		self.client.request.assert_not_called()

	def test_auth_error(self):
		self.client.request.return_value = self._resp(401, {})
		with self.assertRaises(ConnectorAuthError):
			self.adapter.get_messages(account_id="A", sender_id="S")

	def test_invalid_envelope_raises(self):
		self.client.request.return_value = self._resp(200, {"ok": True})
		with self.assertRaises(ConnectorError):
			self.adapter.get_messages(account_id="A", sender_id="S")

	def test_nullable_fields_pass_through(self):
		payload = _messages_envelope(
			messages=[_message(message_id=None, text=None, attachments=[])]
		)
		self.client.request.return_value = self._resp(200, payload)
		data = self.adapter.get_messages(account_id="IG_BIZ", sender_id="IGSID_1")
		self.assertIsNone(data["messages"][0]["message_id"])
		self.assertIsNone(data["messages"][0]["text"])


if __name__ == "__main__":
	unittest.main()

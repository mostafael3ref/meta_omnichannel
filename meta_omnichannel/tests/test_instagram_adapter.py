"""Instagram list adapter tests (mocked HTTP — no production connector)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from meta_omnichannel.instagram.adapter import (
	CONVERSATIONS_PATH,
	InstagramConnectorAdapter,
	conversation_items_ready_for_sync,
	validate_conversations_list_envelope,
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


if __name__ == "__main__":
	unittest.main()

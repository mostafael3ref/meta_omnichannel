"""Facebook message timestamp parsing, insert, and NULL-only repair (mocked frappe)."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


class TestFacebookTimestampUpsert(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe = MagicMock()
		frappe.db = MagicMock()
		frappe.get_doc = MagicMock()
		sys.modules["frappe"] = frappe
		utils = types.ModuleType("frappe.utils")
		utils.cint = int  # type: ignore[attr-defined]
		utils.now_datetime = MagicMock(return_value=datetime(2026, 11, 19, 12, 0, 0))  # type: ignore[attr-defined]
		utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
		sys.modules["frappe.utils"] = utils
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod
		cls.frappe = frappe
		cls.utils = utils

	def setUp(self):
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.set_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.utils.get_system_timezone.return_value = "UTC"
		doc = MagicMock()
		doc.name = "MSG-NEW"
		self.frappe.get_doc.return_value = doc

	def _inserted(self):
		return self.frappe.get_doc.call_args.args[0]

	def test_unix_seconds_sets_sent_at(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-unix",
				"text": "hi",
				"timestamp": 1_795_014_521,
			},
		)
		fields = self._inserted()
		self.assertEqual(fields["sent_at"], datetime(2026, 11, 18, 15, 8, 41))

	def test_unix_milliseconds_sets_sent_at(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-ms",
				"timestamp": 1_795_014_521_000,
			},
		)
		fields = self._inserted()
		self.assertEqual(fields["sent_at"], datetime(2026, 11, 18, 15, 8, 41))

	def test_iso_timestamp_sets_sent_at(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-iso",
				"timestamp": "2026-11-18T15:08:41Z",
			},
		)
		fields = self._inserted()
		self.assertEqual(fields["sent_at"], datetime(2026, 11, 18, 15, 8, 41))

	def test_system_timezone_conversion_on_insert(self):
		self.utils.get_system_timezone.return_value = "Africa/Cairo"
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-tz",
				"timestamp": 1_795_014_521,
			},
		)
		fields = self._inserted()
		self.assertEqual(fields["sent_at"], datetime(2026, 11, 18, 17, 8, 41))

	def test_absent_timestamp_falls_back_to_now(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"direction": "inbound", "message_id": "m-absent", "text": "x"},
		)
		fields = self._inserted()
		self.assertEqual(fields["sent_at"], datetime(2026, 11, 19, 12, 0, 0))

	def test_invalid_timestamp_does_not_invent_now(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-bad",
				"timestamp": "not-a-timestamp",
			},
		)
		fields = self._inserted()
		self.assertIsNone(fields["sent_at"])

	def test_null_sent_at_repaired_on_resync(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if (
				isinstance(filters, dict)
				and filters.get("external_message_id") == "m-repair"
				and filters.get("conversation") == "CONV"
			):
				return SimpleNamespace(name="MSG-LEGACY", sent_at=None)
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-repair",
				"timestamp": 1_795_014_521,
				"text": "ignored",
			},
		)
		self.assertEqual(name, "MSG-LEGACY")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.set_value.assert_called_once_with(
			"Omnichannel Message",
			"MSG-LEGACY",
			"sent_at",
			datetime(2026, 11, 18, 15, 8, 41),
			update_modified=False,
		)

	def test_valid_outbound_sent_at_not_overwritten(self):
		existing_ts = datetime(2026, 11, 18, 10, 0, 0)

		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if (
				isinstance(filters, dict)
				and filters.get("external_message_id") == "m-out"
				and filters.get("conversation") == "CONV"
			):
				return SimpleNamespace(name="MSG-OUT", sent_at=existing_ts)
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "outbound",
				"message_id": "m-out",
				"timestamp": 1_795_014_521,
				"text": "from connector echo",
			},
		)
		self.assertEqual(name, "MSG-OUT")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.get_doc.assert_not_called()

	def test_idempotent_repair_second_pass_noop_when_filled(self):
		filled = datetime(2026, 11, 18, 15, 8, 41)

		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if isinstance(filters, dict) and filters.get("external_message_id") == "m-idem":
				return SimpleNamespace(name="MSG-IDEM", sent_at=filled)
			return None

		self.frappe.db.get_value.side_effect = get_value
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"message_id": "m-idem", "timestamp": 1_795_014_521, "direction": "inbound"},
		)
		self.frappe.db.set_value.assert_not_called()

	def test_conversation_last_message_at_uses_shared_parser(self):
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		doc = MagicMock()
		doc.name = "CONV-NEW"
		self.frappe.get_doc.return_value = doc
		self.sync.upsert_conversation_from_connector(
			{
				"page_id": "P1",
				"sender_id": "S1",
				"last_message": "hello",
				"last_message_at": 1_795_014_521,
			}
		)
		fields = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(fields["last_message_at"], datetime(2026, 11, 18, 15, 8, 41))

	def test_conversation_invalid_last_message_at_omitted(self):
		self.frappe.db.get_value.return_value = None
		doc = MagicMock()
		doc.name = "CONV-NEW"
		self.frappe.get_doc.return_value = doc
		self.sync.upsert_conversation_from_connector(
			{
				"page_id": "P1",
				"sender_id": "S1",
				"last_message_at": "bogus",
			}
		)
		fields = self.frappe.get_doc.call_args.args[0]
		self.assertNotIn("last_message_at", fields)


class TestInboxMessageOrderingSource(unittest.TestCase):
	def test_list_messages_has_stable_tie_breakers(self):
		path = (
			Path(__file__).resolve().parents[1]
			/ "api"
			/ "inbox.py"
		)
		text = path.read_text(encoding="utf-8")
		self.assertIn(
			'order_by="sent_at asc, creation asc, name asc"',
			text,
		)


if __name__ == "__main__":
	unittest.main()

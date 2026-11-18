"""Instagram sync gating and optional-field mapping tests (frappe mocked)."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	sys.modules["frappe"] = frappe
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-18 10:00:00")  # type: ignore[attr-defined]

	def _get_datetime(value):
		if isinstance(value, datetime):
			return value
		if isinstance(value, str) and value.startswith("20"):
			return datetime.fromisoformat(value.replace("Z", "+00:00").replace("+00:00", ""))
		raise ValueError("invalid")

	utils.get_datetime = _get_datetime  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


def _envelope(conversations):
	return {
		"tenant_id": "t1",
		"provider": "instagram",
		"total": len(conversations),
		"conversations": conversations,
		"limit": 50,
		"offset": 0,
		"has_more": False,
		"next_offset": None,
	}


class TestInstagramSyncGating(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.instagram.sync as sync_mod
		import meta_omnichannel.utils.sync_log as sync_log_mod

		importlib.reload(sync_log_mod)
		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		self.frappe.reset_mock()
		self.frappe.db.get_value.return_value = None

	def test_empty_list_succeeds_with_zero_processed(self):
		adapter = MagicMock()
		adapter.list_conversations.return_value = _envelope([])

		with patch.object(self.sync, "InstagramConnectorAdapter", return_value=adapter), patch.object(
			self.sync, "start_sync_log", return_value="LOG-EMPTY"
		), patch.object(self.sync, "finish_sync_log") as finish, patch.object(
			self.sync, "get_logger", return_value=MagicMock()
		):
			result = self.sync.sync_instagram_conversations(limit=10)

		self.assertTrue(result["ok"])
		self.assertEqual(result["processed"], 0)
		self.assertNotIn("sync_blocked", result)
		self.assertEqual(finish.call_args.kwargs["status"], "Success")

	def test_blocks_when_item_schema_missing_identity(self):
		adapter = MagicMock()
		adapter.list_conversations.return_value = _envelope([{"foo": "bar"}])

		with patch.object(self.sync, "InstagramConnectorAdapter", return_value=adapter), patch.object(
			self.sync, "start_sync_log", return_value="LOG-1"
		), patch.object(self.sync, "finish_sync_log") as finish, patch.object(
			self.sync, "get_logger", return_value=MagicMock()
		):
			result = self.sync.sync_instagram_conversations(limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["sync_blocked"])
		self.assertIn("account_id", result["missing_item_fields"])
		self.assertIn("sender_id", result["missing_item_fields"])
		self.assertEqual(finish.call_args.kwargs["status"], "Failed")

	def test_processes_when_identity_fields_present(self):
		channel_doc = MagicMock()
		channel_doc.name = "CH-1"
		conv_doc = MagicMock()
		conv_doc.name = "CONV-1"
		self.frappe.get_doc.side_effect = [channel_doc, conv_doc]

		adapter = MagicMock()
		adapter.list_conversations.return_value = _envelope(
			[{"account_id": "IG_ACCT", "sender_id": "IGSID_9"}]
		)

		with patch.object(self.sync, "InstagramConnectorAdapter", return_value=adapter), patch.object(
			self.sync, "start_sync_log", return_value="LOG-2"
		), patch.object(self.sync, "finish_sync_log") as finish, patch.object(
			self.sync, "get_logger", return_value=MagicMock()
		):
			result = self.sync.sync_instagram_conversations(limit=10)

		self.assertTrue(result["ok"])
		self.assertEqual(result["processed"], 1)
		self.assertEqual(finish.call_args.kwargs["status"], "Success")


class TestInstagramUpsertOptionalFields(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.instagram.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		self.frappe.reset_mock()
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None

	def _upsert(self, row, *, existing_name=None):
		self.frappe.db.get_value.side_effect = [
			"CH-EXISTING",  # channel lookup
			existing_name,  # conversation lookup
		]
		if existing_name is None:
			doc = MagicMock()
			doc.name = "CONV-NEW"
			self.frappe.get_doc.return_value = doc
		return self.sync.upsert_instagram_conversation(row)

	def test_preview_truncated_to_500(self):
		long_preview = "x" * 600
		self._upsert(
			{
				"account_id": "A1",
				"sender_id": "S1",
				"last_message": long_preview,
			}
		)
		kwargs = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(len(kwargs["last_message_preview"]), 500)
		self.assertEqual(kwargs["last_message_preview"], "x" * 500)

	def test_valid_iso_timestamp(self):
		self._upsert(
			{
				"account_id": "A1",
				"sender_id": "S1",
				"last_message": "hello",
				"last_message_at": "2026-11-18T12:00:00Z",
			}
		)
		kwargs = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(kwargs["last_message_preview"], "hello")
		self.assertEqual(kwargs["last_message_at"], datetime(2026, 11, 18, 12, 0, 0))

	def test_valid_unix_timestamp(self):
		self._upsert(
			{
				"account_id": "A1",
				"sender_id": "S1",
				"last_message_at": 1_700_000_000,
			}
		)
		kwargs = self.frappe.get_doc.call_args.args[0]
		self.assertIn("last_message_at", kwargs)
		self.assertIsInstance(kwargs["last_message_at"], datetime)

	def test_missing_optional_fields_still_insert(self):
		self._upsert({"account_id": "A1", "sender_id": "S1"})
		kwargs = self.frappe.get_doc.call_args.args[0]
		self.assertNotIn("last_message_preview", kwargs)
		self.assertNotIn("last_message_at", kwargs)

	def test_invalid_timestamp_omitted(self):
		self._upsert(
			{
				"account_id": "A1",
				"sender_id": "S1",
				"last_message": "ok",
				"last_message_at": "not-a-timestamp",
			}
		)
		kwargs = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(kwargs["last_message_preview"], "ok")
		self.assertNotIn("last_message_at", kwargs)

	def test_empty_optional_does_not_overwrite_existing(self):
		self._upsert(
			{
				"account_id": "A1",
				"sender_id": "S1",
				"last_message": "   ",
				"last_message_at": None,
			},
			existing_name="CONV-EXIST",
		)
		values = self.frappe.db.set_value.call_args.args[2]
		self.assertNotIn("last_message_preview", values)
		self.assertNotIn("last_message_at", values)


if __name__ == "__main__":
	unittest.main()

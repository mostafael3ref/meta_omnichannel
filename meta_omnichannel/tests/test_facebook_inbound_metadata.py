"""Inbound conversation metadata: last_inbound_at, watermarked unread, mark-read races."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _install_sync_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	sys.modules["frappe"] = frappe
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value=datetime(2026, 11, 19, 12, 0, 0))  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


def _install_inbox_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	frappe.session = MagicMock(user="agent@example.com")
	frappe.get_roles = MagicMock(return_value=["Omnichannel Agent"])
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	frappe.get_list = MagicMock(return_value=[])
	frappe.get_all = MagicMock(return_value=[])
	frappe.get_doc = MagicMock()
	frappe.db = MagicMock()
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	return frappe


class TestInboundConversationMetadata(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_sync_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		self.frappe.db.reset_mock()
		self.frappe.get_doc.reset_mock()
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.count.return_value = 0
		self.frappe.db.sql.return_value = None
		doc = MagicMock()
		doc.name = "MSG-NEW"
		self.frappe.get_doc.return_value = doc

	def _upsert_inbound(self, message_id: str, timestamp, *, count_unread=False, watermark=None):
		return self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": message_id,
				"text": "hi",
				"timestamp": timestamp,
			},
			count_unread=count_unread,
			unread_watermark=watermark,
		)

	def _sql_statements(self) -> list[str]:
		return [c.args[0] for c in self.frappe.db.sql.call_args_list if c.args]

	def test_valid_inbound_advances_last_inbound_at(self):
		self._upsert_inbound("m-tip", 1_795_014_521)
		sql = "\n".join(self._sql_statements())
		self.assertIn("last_inbound_at", sql)
		self.assertIn("last_inbound_at IS NULL OR last_inbound_at <", sql)
		# Default: no unread without count_unread + watermark
		self.assertNotIn("unread_count", sql)

	def test_post_seed_tip_increments_unread_when_newer_than_watermark(self):
		watermark = datetime(2026, 11, 18, 10, 0, 0)
		self._upsert_inbound(
			"m-new",
			1_795_014_521,  # 2026-11-18 15:08:41 UTC
			count_unread=True,
			watermark=watermark,
		)
		sql = "\n".join(self._sql_statements())
		self.assertIn("last_inbound_at", sql)
		self.assertIn("unread_count = IFNULL(unread_count, 0) + 1", sql)

	def test_historical_backfill_before_watermark_skips_unread(self):
		watermark = datetime(2026, 11, 18, 16, 0, 0)
		self._upsert_inbound(
			"m-old",
			1_795_014_521,  # before watermark
			count_unread=True,
			watermark=watermark,
		)
		sql = "\n".join(self._sql_statements())
		self.assertIn("last_inbound_at", sql)
		self.assertNotIn("unread_count", sql)

	def test_initial_import_never_increments_unread(self):
		# Even with a watermark passed, callers use count_unread=False on import.
		self._upsert_inbound(
			"m-seed",
			1_795_014_521,
			count_unread=False,
			watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		sql = "\n".join(self._sql_statements())
		self.assertIn("last_inbound_at", sql)
		self.assertNotIn("unread_count", sql)

	def test_duplicate_external_id_skips_metadata(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if (
				isinstance(filters, dict)
				and filters.get("external_message_id") == "m-dup"
				and filters.get("conversation") == "CONV"
			):
				return SimpleNamespace(name="MSG-EXISTING", sent_at=datetime(2026, 11, 18, 15, 8, 41))
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self._upsert_inbound(
			"m-dup",
			1_795_014_521,
			count_unread=True,
			watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		self.assertEqual(name, "MSG-EXISTING")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_fingerprint_dedup_skips_metadata(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if isinstance(filters, dict) and "raw_payload_hash" in filters:
				return "MSG-FP"
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"direction": "inbound", "text": "same", "timestamp": 1_795_014_521},
			count_unread=True,
			unread_watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		self.assertEqual(name, "MSG-FP")
		self.frappe.db.sql.assert_not_called()

	def test_missing_timestamp_skips_last_inbound_and_unread(self):
		self._upsert_inbound(
			"m-absent",
			None,
			count_unread=True,
			watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		# Insert still happens (sent_at falls back to now), but no conversation SQL.
		self.frappe.get_doc.assert_called_once()
		self.frappe.db.sql.assert_not_called()

	def test_invalid_timestamp_skips_last_inbound_and_unread(self):
		self._upsert_inbound(
			"m-bad",
			"not-a-timestamp",
			count_unread=True,
			watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		self.frappe.get_doc.assert_called_once()
		self.frappe.db.sql.assert_not_called()

	def test_outbound_insert_skips_inbound_metadata(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "outbound",
				"message_id": "m-out",
				"timestamp": 1_795_014_521,
				"text": "agent",
			},
			count_unread=True,
			unread_watermark=datetime(2026, 11, 1, 0, 0, 0),
		)
		self.frappe.db.sql.assert_not_called()

	def test_older_inbound_still_issues_monotonic_sql_guard(self):
		"""Older valid inbound still runs the WHERE-guarded UPDATE (no backwards move)."""
		self._upsert_inbound("m-older", 1_700_000_000)  # 2023-11-14
		self.assertEqual(self.frappe.db.sql.call_count, 1)
		sql, params = self.frappe.db.sql.call_args.args[0], self.frappe.db.sql.call_args.args[1]
		self.assertIn("last_inbound_at <", sql)
		self.assertEqual(params["ts"], datetime(2023, 11, 14, 22, 13, 20))

	def test_concurrent_unread_uses_atomic_increment_sql(self):
		watermark = datetime(2026, 11, 1, 0, 0, 0)
		self._upsert_inbound("m-a", 1_795_014_521, count_unread=True, watermark=watermark)
		self._upsert_inbound("m-b", 1_795_014_522, count_unread=True, watermark=watermark)
		unread_sql = [
			c.args[0]
			for c in self.frappe.db.sql.call_args_list
			if c.args and "unread_count = IFNULL" in c.args[0]
		]
		self.assertEqual(len(unread_sql), 2)

	def test_sync_conversation_messages_passes_initial_import_flags(self):
		conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CH-1",
			page_id="P1",
			participant_id="S1",
		)
		self.frappe.get_doc.return_value = conv
		self.frappe.db.count.return_value = 0
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_inbound_at=None,
			last_message_at=None,
		)

		adapter = MagicMock()
		adapter.get_messages.return_value = {
			"items": [
				{
					"direction": "inbound",
					"message_id": "m1",
					"timestamp": 1_795_014_521,
					"text": "seed",
				}
			],
			"has_more": False,
		}

		with patch.object(self.sync, "FacebookConnectorAdapter", return_value=adapter), patch.object(
			self.sync, "start_sync_log", return_value="LOG-1"
		), patch.object(self.sync, "finish_sync_log"), patch.object(
			self.sync, "_maybe_refresh_conversation_preview_from_local_messages"
		), patch.object(self.sync, "upsert_message_from_connector", return_value="MSG-1") as upsert:
			result = self.sync.sync_conversation_messages("CONV-1", limit=10)

		self.assertTrue(result["ok"])
		upsert.assert_called_once()
		kwargs = upsert.call_args.kwargs
		self.assertFalse(kwargs["count_unread"])
		self.assertIsNone(kwargs["unread_watermark"])

	def test_sync_conversation_messages_passes_watermark_for_later_sync(self):
		conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CH-1",
			page_id="P1",
			participant_id="S1",
		)
		self.frappe.get_doc.return_value = conv
		self.frappe.db.count.return_value = 5
		watermark = datetime(2026, 11, 18, 12, 0, 0)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_inbound_at=watermark,
			last_message_at=datetime(2026, 11, 18, 11, 0, 0),
		)

		adapter = MagicMock()
		adapter.get_messages.return_value = {
			"items": [
				{
					"direction": "inbound",
					"message_id": "tip",
					"timestamp": 1_795_014_521,
					"text": "tip",
				}
			],
			"has_more": False,
		}

		with patch.object(self.sync, "FacebookConnectorAdapter", return_value=adapter), patch.object(
			self.sync, "start_sync_log", return_value="LOG-1"
		), patch.object(self.sync, "finish_sync_log"), patch.object(
			self.sync, "_maybe_refresh_conversation_preview_from_local_messages"
		), patch.object(self.sync, "upsert_message_from_connector", return_value="MSG-1") as upsert:
			self.sync.sync_conversation_messages("CONV-1", limit=10)

		kwargs = upsert.call_args.kwargs
		self.assertTrue(kwargs["count_unread"])
		self.assertEqual(kwargs["unread_watermark"], watermark)


class TestMarkConversationRead(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_inbox_frappe()
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm
		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(inbox_mod)
		cls.inbox = inbox_mod
		cls.perm = perm

	def setUp(self):
		self.frappe.db.reset_mock()
		self.frappe.get_doc.reset_mock()
		self.frappe.get_all.reset_mock()
		self.frappe.throw.reset_mock(side_effect=True)
		self.frappe.throw.side_effect = Exception("throw")
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.reset_mock()
		self.perm.has_conversation_permission.return_value = True
		conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CH",
			participant_name="A",
			participant_id="S1",
			status="Open",
			assigned_to=None,
			unread_count=3,
			last_message_at=datetime(2026, 11, 18, 15, 0, 0),
			last_message_preview="hi",
			last_inbound_at=datetime(2026, 11, 18, 15, 0, 0),
			crm_contact=None,
			crm_lead=None,
			page_id="P1",
		)
		self.conv = conv
		self.frappe.get_doc.return_value = conv
		self.frappe.get_all.return_value = []
		self.frappe.db.count.return_value = 0
		self.frappe.db.get_value.return_value = 0

	def test_list_messages_does_not_clear_unread(self):
		result = self.inbox.list_messages("CONV-1", limit=10, offset=0)
		self.frappe.db.set_value.assert_not_called()
		self.assertEqual(result["conversation"]["unread_count"], 3)
		self.assertEqual(
			result["conversation"]["last_inbound_at"], datetime(2026, 11, 18, 15, 0, 0)
		)

	def test_mark_conversation_read_without_as_of_is_noop(self):
		"""Missing as_of must not clear — delayed requests would wipe newer tips."""
		self.frappe.db.get_value.return_value = 3
		result = self.inbox.mark_conversation_read("CONV-1")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.sql.assert_not_called()
		self.assertEqual(result["unread_count"], 3)
		self.assertFalse(result["updated"])

	def test_mark_conversation_read_invalid_as_of_is_noop(self):
		self.frappe.db.get_value.return_value = 3
		result = self.inbox.mark_conversation_read("CONV-1", as_of="not-a-timestamp")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.sql.assert_not_called()
		self.assertEqual(result["unread_count"], 3)
		self.assertFalse(result["updated"])

	def test_mark_conversation_read_with_as_of_recomputes(self):
		self.frappe.db.get_value.return_value = 1
		as_of = "2026-11-18T15:00:00Z"
		result = self.inbox.mark_conversation_read("CONV-1", as_of=as_of)
		self.frappe.db.set_value.assert_not_called()
		self.assertTrue(self.frappe.db.sql.called)
		sql, params = self.frappe.db.sql.call_args.args[0], self.frappe.db.sql.call_args.args[1]
		self.assertIn("sent_at > %(as_of)s", sql)
		self.assertIn("direction = 'Inbound'", sql)
		self.assertEqual(params["name"], "CONV-1")
		self.assertEqual(params["as_of"], datetime(2026, 11, 18, 15, 0, 0))
		self.assertEqual(result["unread_count"], 1)
		self.assertTrue(result["updated"])

	def test_mark_conversation_read_race_keeps_newer_inbound(self):
		"""Delayed mark-read with stale as_of still counts tips after the watermark."""
		self.frappe.db.get_value.return_value = 2
		result = self.inbox.mark_conversation_read(
			"CONV-1", as_of=datetime(2026, 11, 18, 14, 0, 0)
		)
		sql = self.frappe.db.sql.call_args.args[0]
		self.assertIn("COUNT(*)", sql)
		self.assertIn("sent_at > %(as_of)s", sql)
		self.assertEqual(result["unread_count"], 2)
		self.assertTrue(result["updated"])

	def test_mark_conversation_read_permission_denied(self):
		self.perm.has_conversation_permission.return_value = False
		with self.assertRaises(Exception):
			self.inbox.mark_conversation_read("CONV-1", as_of="2026-11-18T15:00:00Z")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_mark_conversation_read_requires_agent(self):
		self.perm.ensure_omnichannel_agent.side_effect = Exception("not agent")
		with self.assertRaises(Exception):
			self.inbox.mark_conversation_read("CONV-1")
		self.frappe.db.set_value.assert_not_called()
		self.perm.ensure_omnichannel_agent.side_effect = None


class TestFrontendMarkReadWiring(unittest.TestCase):
	def test_open_calls_mark_read_soft_refresh_does_not(self):
		from pathlib import Path

		js = (
			Path(__file__).resolve().parents[1]
			/ "public"
			/ "frontend"
			/ "omnichannel.js"
		).read_text(encoding="utf-8")
		soft_start = js.index("async function softRefreshMessages")
		open_start = js.index("async function openConversation")
		soft = js[soft_start:open_start]
		# soft ends before open; open body until sendMessage
		send_start = js.index("async function sendMessage")
		open_body = js[open_start:send_start]

		self.assertIn("mark_conversation_read", open_body)
		self.assertIn("as_of: asOf", open_body)
		self.assertIn("toISOString()", open_body)
		self.assertNotIn("mark_conversation_read", soft)
		self.assertNotIn("unread_count = 0", soft)
		self.assertIn("msgData.conversation", soft)


if __name__ == "__main__":
	unittest.main()

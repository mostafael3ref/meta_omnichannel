"""Instagram message sync loop tests (pagination, ordering, partial failure)."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from meta_omnichannel.meta_connectors.errors import ConnectorError


class _AfterCommit:
	def __init__(self):
		self.callbacks: list = []

	def add(self, fn):
		self.callbacks.append(fn)

	def run(self):
		for fn in list(self.callbacks):
			fn()
		self.callbacks.clear()


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.db.after_commit = _AfterCommit()
	frappe.get_doc = MagicMock()
	frappe.db.count = MagicMock(return_value=0)
	sys.modules["frappe"] = frappe

	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.get_datetime = lambda v: v  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-18 12:00:00")  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


def _msg(mid: str, text: str, *, ts: str = "2026-11-18T10:00:00Z"):
	return {
		"direction": "inbound",
		"text": text,
		"attachments": [],
		"timestamp": ts,
		"message_id": mid,
	}


def _page(*, messages, offset=0, has_more=False, next_offset=None, account_id="IG_ACCT", sender_id="IGSID_1"):
	return {
		"tenant_id": "t1",
		"provider": "instagram",
		"account_id": account_id,
		"sender_id": sender_id,
		"total": len(messages),
		"messages": messages,
		"limit": 50,
		"offset": offset,
		"has_more": has_more,
		"next_offset": next_offset,
	}


class TestInstagramMessageSyncLoop(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as fb_sync
		import meta_omnichannel.instagram.sync as ig_sync
		import meta_omnichannel.utils.datetimes as dt_mod
		import meta_omnichannel.utils.sync_log as sync_log_mod

		importlib.reload(dt_mod)
		importlib.reload(sync_log_mod)
		importlib.reload(fb_sync)
		importlib.reload(ig_sync)
		cls.ig = ig_sync
		cls.fb = fb_sync

	def setUp(self):
		self.frappe.reset_mock()
		self.frappe.db.count.return_value = 0
		self.frappe.db.after_commit = _AfterCommit()
		self.conv = SimpleNamespace(
			name="CONV-IG",
			provider="Instagram",
			channel="CH-IG",
			page_id="IG_ACCT",
			participant_id="IGSID_1",
		)
		self.frappe.get_doc.return_value = self.conv
		self.resolved = SimpleNamespace(
			name="CONV-IG",
			channel="CH-IG",
			provider="Instagram",
			page_id="IG_ACCT",
			participant_id="IGSID_1",
		)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_inbound_at=None,
			last_message_at=None,
		)

	def _run(self, adapter, **kwargs):
		with patch.object(self.ig, "InstagramConnectorAdapter", return_value=adapter), patch.object(
			self.ig, "resolve_existing_instagram_conversation", return_value=self.resolved
		), patch.object(self.ig, "start_sync_log", return_value="LOG-IG"), patch.object(
			self.ig, "finish_sync_log"
		) as finish, patch.object(self.ig, "get_logger", return_value=MagicMock()):
			result = self.ig.sync_instagram_conversation_messages("CONV-IG", **kwargs)
		return result, finish

	def test_one_page_success(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "a"), _msg("m2", "b")],
			has_more=False,
		)
		upsert = MagicMock(
			side_effect=[
				{"name": "MSG-1", "status": "inserted"},
				{"name": "MSG-2", "status": "inserted"},
			]
		)
		with patch.object(self.ig, "upsert_instagram_message_from_connector", upsert):
			result, finish = self._run(adapter, limit=10, max_pages=3)

		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])
		self.assertEqual(result["inserted"], 2)
		self.assertEqual(result["duplicates"], 0)
		self.assertEqual(result["pages"], 1)
		adapter.get_messages.assert_called_once_with(
			account_id="IG_ACCT", sender_id="IGSID_1", limit=10, offset=0
		)
		self.assertEqual(finish.call_args.kwargs["status"], "Success")

	def test_multiple_pages_oldest_to_newest_import(self):
		"""offset=0 newest page; import order is older page then newer page."""
		adapter = MagicMock()
		newer = [_msg("n1", "newer-old"), _msg("n2", "newer-new")]
		older = [_msg("o1", "older-old"), _msg("o2", "older-new")]
		adapter.get_messages.side_effect = [
			_page(messages=newer, offset=0, has_more=True, next_offset=2),
			_page(messages=older, offset=2, has_more=False, next_offset=None),
		]
		order: list[str] = []

		def _upsert(*, row, **_kwargs):
			order.append(row["message_id"])
			return {"name": f"MSG-{row['message_id']}", "status": "inserted"}

		with patch.object(self.ig, "upsert_instagram_message_from_connector", side_effect=_upsert):
			result, _ = self._run(adapter, limit=2, max_pages=5)

		self.assertTrue(result["ok"])
		self.assertEqual(result["pages"], 2)
		self.assertEqual(result["inserted"], 4)
		self.assertEqual(order, ["o1", "o2", "n1", "n2"])
		self.assertEqual(
			adapter.get_messages.call_args_list,
			[
				call(account_id="IG_ACCT", sender_id="IGSID_1", limit=2, offset=0),
				call(account_id="IG_ACCT", sender_id="IGSID_1", limit=2, offset=2),
			],
		)

	def test_max_pages_hard_limit(self):
		adapter = MagicMock()
		adapter.get_messages.side_effect = [
			_page(messages=[_msg("a", "1")], offset=0, has_more=True, next_offset=1),
			_page(messages=[_msg("b", "2")], offset=1, has_more=True, next_offset=2),
			_page(messages=[_msg("c", "3")], offset=2, has_more=True, next_offset=3),
		]
		with patch.object(
			self.ig,
			"upsert_instagram_message_from_connector",
			return_value={"name": "MSG", "status": "inserted"},
		):
			result, _ = self._run(adapter, limit=1, max_pages=2)

		self.assertTrue(result["ok"])
		self.assertEqual(result["pages"], 2)
		self.assertEqual(adapter.get_messages.call_count, 2)

	def test_repeated_offset_marks_partial(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "x")],
			offset=0,
			has_more=True,
			next_offset=0,  # repeated
		)
		with patch.object(
			self.ig,
			"upsert_instagram_message_from_connector",
			return_value={"name": "MSG-1", "status": "inserted"},
		):
			result, finish = self._run(adapter, limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["error"], "repeated_offset")
		self.assertEqual(result["inserted"], 1)
		self.assertEqual(result["pages"], 1)
		self.assertEqual(finish.call_args.kwargs["status"], "Failed")

	def test_invalid_next_offset_marks_partial(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "x")],
			has_more=True,
			next_offset="nope",
		)
		with patch.object(
			self.ig,
			"upsert_instagram_message_from_connector",
			return_value={"name": "MSG-1", "status": "inserted"},
		):
			result, _ = self._run(adapter)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["error"], "invalid_next_offset")
		self.assertEqual(result["inserted"], 1)

	def test_duplicate_counts(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "a"), _msg("m2", "b"), _msg("m3", "c")],
			has_more=False,
		)
		upsert = MagicMock(
			side_effect=[
				{"name": "MSG-1", "status": "inserted"},
				{"name": "MSG-2", "status": "duplicate"},
				{"name": "MSG-3", "status": "inserted"},
			]
		)
		with patch.object(self.ig, "upsert_instagram_message_from_connector", upsert):
			result, _ = self._run(adapter)

		self.assertTrue(result["ok"])
		self.assertEqual(result["inserted"], 2)
		self.assertEqual(result["duplicates"], 1)
		self.assertEqual(result["processed"], 3)

	def test_connector_error_before_pages_is_failure(self):
		adapter = MagicMock()
		adapter.get_messages.side_effect = ConnectorError("boom")
		result, finish = self._run(adapter)
		self.assertFalse(result["ok"])
		self.assertFalse(result["partial"])
		self.assertEqual(result["pages"], 0)
		self.assertIn("boom", result["error"])
		self.assertEqual(finish.call_args.kwargs["status"], "Failed")

	def test_connector_error_after_page_is_partial(self):
		adapter = MagicMock()
		adapter.get_messages.side_effect = [
			_page(messages=[_msg("m1", "kept")], has_more=True, next_offset=1),
			ConnectorError("page2 failed"),
		]
		with patch.object(
			self.ig,
			"upsert_instagram_message_from_connector",
			return_value={"name": "MSG-1", "status": "inserted"},
		):
			result, _ = self._run(adapter, limit=1)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["inserted"], 1)
		self.assertEqual(result["pages"], 1)
		self.assertIn("page2 failed", result["error"])

	def test_upsert_exception_marks_partial(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "a"), _msg("m2", "b")],
			has_more=False,
		)
		upsert = MagicMock(
			side_effect=[
				{"name": "MSG-1", "status": "inserted"},
				RuntimeError("write failed"),
			]
		)
		with patch.object(self.ig, "upsert_instagram_message_from_connector", upsert):
			result, _ = self._run(adapter)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["inserted"], 1)
		self.assertIn("write failed", result["error"])

	def test_identity_mismatch_rejected(self):
		adapter = MagicMock()
		with patch.object(self.ig, "InstagramConnectorAdapter", return_value=adapter), patch.object(
			self.ig, "resolve_existing_instagram_conversation", return_value=None
		), patch.object(self.ig, "start_sync_log", return_value="LOG-IG"), patch.object(
			self.ig, "finish_sync_log"
		):
			result = self.ig.sync_instagram_conversation_messages("CONV-IG")

		self.assertFalse(result["ok"])
		self.assertEqual(result["error"], "identity_mismatch")
		adapter.get_messages.assert_not_called()

	def test_envelope_identity_mismatch_partial(self):
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(
			messages=[_msg("m1", "x")],
			account_id="OTHER",
			sender_id="IGSID_1",
			has_more=False,
		)
		with patch.object(self.ig, "upsert_instagram_message_from_connector") as upsert:
			result, _ = self._run(adapter)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["error"], "envelope_identity_mismatch")
		upsert.assert_not_called()

	def test_preserves_unread_watermark_flags(self):
		self.frappe.db.count.return_value = 4
		watermark = datetime(2026, 11, 18, 9, 0, 0)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_inbound_at=watermark,
			last_message_at=None,
		)
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(messages=[_msg("tip", "hi")], has_more=False)
		upsert = MagicMock(return_value={"name": "MSG", "status": "inserted"})
		with patch.object(self.ig, "upsert_instagram_message_from_connector", upsert):
			result, _ = self._run(adapter)

		self.assertTrue(result["ok"])
		self.assertTrue(result["count_unread"])
		kwargs = upsert.call_args.kwargs
		self.assertTrue(kwargs["count_unread"])
		self.assertEqual(kwargs["unread_watermark"], watermark)

	def test_initial_import_disables_unread(self):
		self.frappe.db.count.return_value = 0
		adapter = MagicMock()
		adapter.get_messages.return_value = _page(messages=[_msg("m1", "seed")], has_more=False)
		upsert = MagicMock(return_value={"name": "MSG", "status": "inserted"})
		with patch.object(self.ig, "upsert_instagram_message_from_connector", upsert):
			result, _ = self._run(adapter)

		self.assertFalse(result["count_unread"])
		self.assertFalse(upsert.call_args.kwargs["count_unread"])

	def test_facebook_sync_conversation_messages_regression(self):
		"""Facebook message sync loop still loads, paginates, and upserts."""
		conv = SimpleNamespace(
			name="CONV-FB",
			provider="Facebook",
			channel="CH-FB",
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
			"messages": [{"direction": "inbound", "message_id": "fb1", "text": "x"}],
			"has_more": False,
		}
		with patch.object(self.fb, "FacebookConnectorAdapter", return_value=adapter), patch.object(
			self.fb, "start_sync_log", return_value="LOG-FB"
		), patch.object(self.fb, "finish_sync_log"), patch.object(
			self.fb, "_maybe_refresh_conversation_preview_from_local_messages"
		), patch.object(
			self.fb, "upsert_message_from_connector", return_value="MSG-FB"
		) as upsert:
			result = self.fb.sync_conversation_messages("CONV-FB", limit=10)

		self.assertTrue(result["ok"])
		self.assertEqual(result["processed"], 1)
		upsert.assert_called_once()
		adapter.get_messages.assert_called_once_with(
			page_id="P1", sender_id="S1", limit=10, offset=0
		)


class TestResolveNextMessageOffset(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		_install_fake_frappe()
		import meta_omnichannel.instagram.sync as ig_sync

		importlib.reload(ig_sync)
		cls.ig = ig_sync

	def test_clean_stop_when_no_more(self):
		nxt, err = self.ig._resolve_next_message_offset(
			current_offset=0,
			limit=50,
			payload={"has_more": False, "next_offset": 50},
			seen_offsets={0},
		)
		self.assertIsNone(nxt)
		self.assertIsNone(err)

	def test_default_step_when_next_missing(self):
		nxt, err = self.ig._resolve_next_message_offset(
			current_offset=0,
			limit=25,
			payload={"has_more": True, "next_offset": None},
			seen_offsets={0},
		)
		self.assertEqual(nxt, 25)
		self.assertIsNone(err)

	def test_rejects_backward_offset(self):
		nxt, err = self.ig._resolve_next_message_offset(
			current_offset=10,
			limit=10,
			payload={"has_more": True, "next_offset": 5},
			seen_offsets={0, 10},
		)
		self.assertIsNone(nxt)
		self.assertEqual(err, "invalid_next_offset")


if __name__ == "__main__":
	unittest.main()

"""Instagram inbox coordinator: lock, fair selection, bounded sync, inbox wiring."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe.cache = MagicMock()
	cache = MagicMock()
	cache.get_value.return_value = None
	cache.set_value.return_value = None
	cache.delete_value.return_value = None
	cache.make_key = lambda key, *a, **k: f"site|{key}"
	cache.set = MagicMock(return_value=True)
	cache.eval = MagicMock(return_value=1)
	frappe.cache.return_value = cache
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-19 12:00:00")  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	return frappe


def _load_instagram_sync():
	"""Reload Instagram sync against real facebook_messenger.sync helpers (frappe mocked)."""
	import meta_omnichannel.facebook_messenger.sync as fb_sync
	import meta_omnichannel.instagram.sync as sync_mod
	import meta_omnichannel.utils.sync_log as sync_log_mod

	importlib.reload(sync_log_mod)
	importlib.reload(fb_sync)
	importlib.reload(sync_mod)
	return sync_mod


class TestAtomicInstagramInboxLock(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		cls.sync = _load_instagram_sync()

	def setUp(self):
		cache = MagicMock()
		cache.make_key = lambda key, *a, **k: f"site|{key}"
		cache.set = MagicMock(return_value=True)
		cache.eval = MagicMock(return_value=1)
		cache.get_value.return_value = None
		self.frappe.cache.return_value = cache
		self.cache = cache

	def test_acquire_uses_instagram_lock_key_and_nx_ex(self):
		token = self.sync._try_acquire_instagram_inbox_sync_lock()
		self.assertTrue(token)
		self.cache.set.assert_called_once()
		args, kwargs = self.cache.set.call_args
		self.assertTrue(kwargs.get("nx"))
		self.assertEqual(kwargs.get("ex"), self.sync.INSTAGRAM_INBOX_SYNC_LOCK_TTL_SEC)
		key_arg = args[0] if args else kwargs.get("name") or kwargs.get("key")
		self.assertIn("instagram_inbox_sync", str(key_arg))
		self.assertNotIn("facebook_inbox_sync", str(key_arg))

	def test_lock_key_constant_is_instagram_specific(self):
		self.assertEqual(
			self.sync.INSTAGRAM_INBOX_SYNC_LOCK_KEY, "omnichannel:instagram_inbox_sync"
		)
		self.assertNotEqual(
			self.sync.INSTAGRAM_INBOX_SYNC_LOCK_KEY, "omnichannel:facebook_inbox_sync"
		)
		self.assertEqual(
			self.sync.INSTAGRAM_FAIR_CURSOR_KEY,
			"omnichannel:instagram_message_sync_fair_cursor",
		)

	def test_contention_returns_none(self):
		self.cache.set.return_value = False
		self.assertIsNone(self.sync._try_acquire_instagram_inbox_sync_lock())

	def test_release_uses_ownership_token(self):
		self.sync._release_instagram_inbox_sync_lock("abc123")
		self.cache.eval.assert_called_once()
		args = self.cache.eval.call_args.args
		self.assertIn("abc123", args)

	def test_release_without_token_is_noop(self):
		self.sync._release_instagram_inbox_sync_lock(None)
		self.cache.eval.assert_not_called()

	def test_redis_failure_fails_closed(self):
		self.cache.set.side_effect = RuntimeError("redis down")
		self.assertIsNone(self.sync._try_acquire_instagram_inbox_sync_lock())


class TestInstagramFairSelection(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		cls.sync = _load_instagram_sync()

	def setUp(self):
		self.frappe.db.sql.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_all.reset_mock(side_effect=True, return_value=True)
		cache = MagicMock()
		cache.get_value.return_value = None
		cache.set_value = MagicMock()
		cache.make_key = lambda key, *a, **k: key
		cache.set = MagicMock(return_value=True)
		self.frappe.cache.return_value = cache
		self.cache = cache

	def test_filters_provider_instagram_only(self):
		self.frappe.db.sql.return_value = []
		self.frappe.get_all.return_value = [SimpleNamespace(name="IG1")]
		self.sync.select_instagram_conversations_for_message_sync(max_conversations=5)
		for call in self.frappe.get_all.call_args_list:
			filters = call.kwargs.get("filters") or {}
			self.assertEqual(filters.get("provider"), "Instagram")
		sql_args = self.frappe.db.sql.call_args.args
		self.assertEqual(sql_args[1][0], "Instagram")

	def test_empty_cannot_consume_entire_batch(self):
		empties = [{"name": f"E{i:02d}"} for i in range(30)]
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(20)]
		alls = [SimpleNamespace(name=f"A{i:02d}") for i in range(40)]

		def get_all(*args, **kwargs):
			order = kwargs.get("order_by") or ""
			if "last_message_at" in order:
				return recents[: kwargs.get("limit_page_length", 20)]
			return alls

		self.frappe.db.sql.return_value = empties
		self.frappe.get_all.side_effect = get_all
		selected = self.sync.select_instagram_conversations_for_message_sync(
			max_conversations=20
		)
		self.assertEqual(len(selected), 20)
		empty_selected = [n for n in selected if n.startswith("E")]
		recent_selected = [n for n in selected if n.startswith("R")]
		self.assertLessEqual(len(empty_selected), 5)
		self.assertGreaterEqual(len(recent_selected), 10)

	def test_deduplicates_selected_names(self):
		dup = [SimpleNamespace(name="SAME")] * 5
		self.frappe.db.sql.return_value = [{"name": "SAME"}]
		self.frappe.get_all.side_effect = lambda *a, **k: dup
		selected = self.sync.select_instagram_conversations_for_message_sync(
			max_conversations=10
		)
		self.assertEqual(selected.count("SAME"), 1)

	def test_caps_at_twenty_by_default(self):
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(50)]
		self.frappe.db.sql.return_value = []
		self.frappe.get_all.side_effect = lambda *a, **k: recents[
			: k.get("limit_page_length", 20)
		]
		selected = self.sync.select_instagram_conversations_for_message_sync()
		self.assertEqual(len(selected), 20)
		self.assertEqual(self.sync.DEFAULT_MAX_MESSAGE_CONVERSATIONS, 20)

	def test_successive_runs_eventually_select_older_conversations(self):
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(30)]
		empties = [{"name": f"E{i:02d}"} for i in range(30)]
		olds = [SimpleNamespace(name=f"Z{i:02d}") for i in range(10)]
		fair_pool = sorted(recents + olds, key=lambda r: r.name)

		cursor_box = {"value": None}

		def get_value(key, *a, **k):
			if key == self.sync.INSTAGRAM_FAIR_CURSOR_KEY:
				return cursor_box["value"]
			return None

		def set_value(key, value, *a, **k):
			if key == self.sync.INSTAGRAM_FAIR_CURSOR_KEY:
				cursor_box["value"] = value

		self.cache.get_value.side_effect = get_value
		self.cache.set_value.side_effect = set_value
		self.frappe.db.sql.return_value = empties

		def get_all(*args, **kwargs):
			order = kwargs.get("order_by") or ""
			lim = kwargs.get("limit_page_length", 20)
			if "last_message_at" in order:
				return recents[:lim]
			return fair_pool

		self.frappe.get_all.side_effect = get_all

		seen_old: set[str] = set()
		for _ in range(12):
			selected = self.sync.select_instagram_conversations_for_message_sync(
				max_conversations=20
			)
			self.assertEqual(len(selected), 20)
			empty_n = sum(1 for n in selected if n.startswith("E"))
			self.assertLessEqual(empty_n, 5)
			seen_old.update(n for n in selected if n.startswith("Z"))
		self.assertTrue(seen_old, "fair cursor must eventually select older Z* conversations")
		self.assertGreaterEqual(len(seen_old), 2)

	def test_fair_cursor_redis_read_failure_does_not_corrupt_selection(self):
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(10)]
		self.frappe.db.sql.return_value = []
		self.frappe.get_all.side_effect = lambda *a, **k: recents[
			: k.get("limit_page_length", 10)
		]
		self.cache.get_value.side_effect = RuntimeError("redis read failed")
		selected = self.sync.select_instagram_conversations_for_message_sync(
			max_conversations=4
		)
		self.assertTrue(selected)
		self.assertLessEqual(len(selected), 4)


class TestSyncInstagramInboxBounded(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		cls.sync = _load_instagram_sync()

	def setUp(self):
		cache = MagicMock()
		cache.make_key = lambda key, *a, **k: f"site|{key}"
		cache.set = MagicMock(return_value=True)
		cache.eval = MagicMock(return_value=1)
		cache.get_value.return_value = None
		cache.set_value = MagicMock()
		self.frappe.cache.return_value = cache
		self.cache = cache

	def test_new_and_existing_conversations_get_message_sync(self):
		list_result = {"ok": True, "processed": 2, "pages": 1, "sync_log": "L1"}
		with patch.object(
			self.sync, "sync_instagram_conversations", return_value=list_result
		), patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["NEW", "EXISTING"],
		), patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			side_effect=[
				{"ok": True, "processed": 3, "pages": 1},
				{"ok": True, "processed": 1, "pages": 1},
			],
		) as msg_sync:
			result = self.sync.sync_instagram_inbox_bounded(limit=50)

		self.assertEqual(msg_sync.call_count, 2)
		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])
		self.assertEqual(result["messages"]["synced"], 2)
		self.assertEqual(result["messages"]["processed_messages"], 4)
		self.cache.eval.assert_called()

	def test_no_new_messages_still_ok(self):
		with patch.object(
			self.sync,
			"sync_instagram_conversations",
			return_value={"ok": True, "processed": 1, "pages": 1},
		), patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["C1"],
		), patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			return_value={"ok": True, "processed": 0, "pages": 1},
		):
			result = self.sync.sync_instagram_inbox_bounded(limit=10)
		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])

	def test_message_failures_mark_partial_not_full_success(self):
		with patch.object(
			self.sync,
			"sync_instagram_conversations",
			return_value={"ok": True, "processed": 1, "pages": 1},
		), patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["OK", "BAD"],
		), patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			side_effect=[
				{"ok": True, "processed": 2},
				{"ok": False, "error": "connector timeout"},
			],
		):
			result = self.sync.sync_instagram_inbox_bounded(limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertIn("1 conversation", result["error"])
		self.assertNotIn("Authorization", result["error"])
		self.assertEqual(result["messages"]["synced"], 1)
		self.assertEqual(len(result["messages"]["failed"]), 1)

	def test_list_failure_with_message_success_is_partial(self):
		with patch.object(
			self.sync,
			"sync_instagram_conversations",
			return_value={"ok": False, "processed": 0, "error": "list down"},
		), patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["C1"],
		), patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			return_value={"ok": True, "processed": 2},
		):
			result = self.sync.sync_instagram_inbox_bounded(limit=10)
		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["messages"]["synced"], 1)

	def test_redacts_sensitive_failure_text(self):
		err = self.sync._safe_message_sync_error(
			"Authorization: Bearer secret-token failed"
		)
		self.assertEqual(err, "message sync failed")

	def test_duplicate_overlapping_run_skipped(self):
		self.cache.set.return_value = False
		with patch.object(self.sync, "sync_instagram_conversations") as list_sync:
			result = self.sync.sync_instagram_inbox_bounded(limit=10)
		list_sync.assert_not_called()
		self.assertTrue(result.get("skipped"))
		self.assertFalse(result["ok"])

	def test_passes_message_pagination_bounds(self):
		with patch.object(
			self.sync,
			"sync_instagram_conversations",
			return_value={"ok": True, "processed": 0, "pages": 1},
		) as list_sync, patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["C1"],
		) as select, patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			return_value={"ok": True, "processed": 0},
		) as msg_sync:
			self.sync.sync_instagram_inbox_bounded(
				limit=25,
				max_conversation_pages=2,
				max_message_conversations=7,
				message_limit=10,
				max_message_pages=1,
			)
		list_sync.assert_called_once_with(limit=25, max_pages=2)
		select.assert_called_once_with(max_conversations=7)
		msg_sync.assert_called_once_with("C1", limit=10, max_pages=1)

	def test_per_conversation_exception_isolated(self):
		with patch.object(
			self.sync,
			"sync_instagram_conversations",
			return_value={"ok": True, "processed": 1},
		), patch.object(
			self.sync,
			"select_instagram_conversations_for_message_sync",
			return_value=["OK", "BOOM"],
		), patch.object(
			self.sync,
			"sync_instagram_conversation_messages",
			side_effect=[
				{"ok": True, "processed": 1},
				RuntimeError("Authorization: Bearer leaked"),
			],
		):
			result = self.sync.sync_instagram_inbox_bounded(limit=5)
		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["messages"]["synced"], 1)
		self.assertEqual(result["messages"]["failed"][0]["error"], "message sync failed")


class TestInstagramInboxWiring(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		cls.frappe._ = lambda x: x
		cls.frappe.whitelist = lambda *a, **k: (lambda fn: fn)
		cls.frappe.PermissionError = type("PermissionError", (Exception,), {})
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm
		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(inbox_mod)
		cls.inbox = inbox_mod

	def test_sync_inbox_instagram_runner_uses_bounded_coordinator(self):
		payload = {
			"ok": True,
			"partial": False,
			"skipped": False,
			"processed": 2,
			"messages": {"synced": 1, "failed": []},
		}
		with patch(
			"meta_omnichannel.instagram.sync.sync_instagram_inbox_bounded",
			return_value=payload,
		) as coord:
			result = self.inbox._sync_instagram_conversations(40)
		coord.assert_called_once_with(limit=40)
		self.assertEqual(result, payload)

	def test_sync_instagram_inbox_whitelist_uses_bounded(self):
		payload = {"ok": True, "processed": 1, "messages": {"synced": 0, "failed": []}}
		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch(
			"meta_omnichannel.instagram.sync.sync_instagram_inbox_bounded",
			return_value=payload,
		) as coord:
			result = self.inbox.sync_instagram_inbox(limit=15)
		coord.assert_called_once_with(limit=15)
		self.assertEqual(result, payload)

	def test_instagram_partial_does_not_block_facebook(self):
		fb = {"ok": True, "processed": 3}
		ig = {
			"ok": False,
			"partial": True,
			"error": "Message sync failed for 2 conversation(s)",
			"processed": 1,
		}
		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		), patch.object(self.inbox, "_sync_instagram_conversations", return_value=ig):
			result = self.inbox.sync_inbox(limit=10)
		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertTrue(result["facebook"]["ok"])
		self.assertIn("Message sync failed", result["error"])

	def test_facebook_partial_still_runs_instagram(self):
		fb = {
			"ok": False,
			"partial": True,
			"error": "Message sync failed for 1 conversation(s)",
		}
		ig = {"ok": True, "partial": False, "processed": 2, "messages": {"synced": 2}}
		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		), patch.object(self.inbox, "_sync_instagram_conversations", return_value=ig):
			result = self.inbox.sync_inbox(limit=10)
		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertTrue(result["instagram"]["ok"])


if __name__ == "__main__":
	unittest.main()

"""Facebook inbox coordinator: lock, fair selection, previews, partial failures."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
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


class TestAtomicInboxLock(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		cache = MagicMock()
		cache.make_key = lambda key, *a, **k: f"site|{key}"
		cache.set = MagicMock(return_value=True)
		cache.eval = MagicMock(return_value=1)
		cache.get_value.return_value = None
		self.frappe.cache.return_value = cache
		self.cache = cache

	def test_acquire_uses_redis_set_nx_ex(self):
		token = self.sync._try_acquire_facebook_inbox_sync_lock()
		self.assertTrue(token)
		self.cache.set.assert_called_once()
		args, kwargs = self.cache.set.call_args
		self.assertTrue(kwargs.get("nx") is True or (len(args) >= 1 and kwargs.get("nx")))
		self.assertEqual(kwargs.get("ex"), self.sync.FACEBOOK_INBOX_SYNC_LOCK_TTL_SEC)
		self.assertTrue(kwargs.get("nx"))

	def test_contention_returns_none(self):
		self.cache.set.return_value = False
		self.assertIsNone(self.sync._try_acquire_facebook_inbox_sync_lock())

	def test_release_uses_ownership_token(self):
		self.sync._release_facebook_inbox_sync_lock("abc123")
		self.cache.eval.assert_called_once()
		args = self.cache.eval.call_args.args
		self.assertIn("abc123", args)

	def test_release_without_token_is_noop(self):
		self.sync._release_facebook_inbox_sync_lock(None)
		self.cache.eval.assert_not_called()

	def test_redis_failure_fails_closed(self):
		self.cache.set.side_effect = RuntimeError("redis down")
		self.assertIsNone(self.sync._try_acquire_facebook_inbox_sync_lock())


class TestPreviewProtection(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def test_merge_rejects_older_candidate(self):
		newer = datetime(2026, 11, 19, 12, 0, 0)
		older = datetime(2026, 11, 18, 12, 0, 0)
		patch = self.sync._merge_conversation_preview_fields(
			existing_at=newer,
			existing_preview="outbound hi",
			candidate_at=older,
			candidate_preview="stale inbound",
		)
		self.assertEqual(patch, {})

	def test_merge_accepts_newer_candidate(self):
		patch = self.sync._merge_conversation_preview_fields(
			existing_at=datetime(2026, 11, 18, 12, 0, 0),
			existing_preview="old",
			candidate_at=datetime(2026, 11, 19, 12, 0, 0),
			candidate_preview="fresh",
		)
		self.assertEqual(patch["last_message_at"], datetime(2026, 11, 19, 12, 0, 0))
		self.assertEqual(patch["last_message_preview"], "fresh")

	def test_merge_equal_timestamp_does_not_replace_existing_preview(self):
		"""Same-second unrelated text must not overwrite an existing preview."""
		ts = datetime(2026, 11, 19, 12, 0, 0)
		patch = self.sync._merge_conversation_preview_fields(
			existing_at=ts,
			existing_preview="outbound agent reply",
			candidate_at=ts,
			candidate_preview="unrelated older inbound",
		)
		self.assertEqual(patch, {})

	def test_merge_equal_timestamp_fills_blank_preview_only(self):
		ts = datetime(2026, 11, 19, 12, 0, 0)
		patch = self.sync._merge_conversation_preview_fields(
			existing_at=ts,
			existing_preview="  ",
			candidate_at=ts,
			candidate_preview="fill me",
		)
		self.assertEqual(patch, {"last_message_preview": "fill me"})

	def test_merge_rejects_missing_candidate_timestamp(self):
		patch = self.sync._merge_conversation_preview_fields(
			existing_at=datetime(2026, 11, 19, 12, 0, 0),
			existing_preview="keep",
			candidate_at=None,
			candidate_preview="nope",
		)
		self.assertEqual(patch, {})

	def test_local_refresh_does_not_roll_back_newer_conversation(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_message_at=datetime(2026, 11, 19, 15, 0, 0),
			last_message_preview="connector fresh",
		)
		self.frappe.db.sql.return_value = [
			SimpleNamespace(
				text_content="older local",
				sent_at=datetime(2026, 11, 18, 10, 0, 0),
				name="MSG1",
			)
		]
		self.frappe.db.set_value.reset_mock()
		self.sync._maybe_refresh_conversation_preview_from_local_messages("CONV")
		self.frappe.db.set_value.assert_not_called()

	def test_local_refresh_equal_timestamp_keeps_existing_preview(self):
		ts = datetime(2026, 11, 19, 12, 0, 0)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			last_message_at=ts,
			last_message_preview="keep outbound",
		)
		self.frappe.db.sql.return_value = [
			SimpleNamespace(text_content="other same-second", sent_at=ts, name="MSG9"),
		]
		self.frappe.db.set_value.reset_mock()
		self.sync._maybe_refresh_conversation_preview_from_local_messages("CONV")
		self.frappe.db.set_value.assert_not_called()


class TestFairSelection(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

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

	def test_empty_cannot_consume_entire_batch(self):
		# 30 permanently empty + recent actives; limit 20 → empties capped ~5.
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
		selected = self.sync.select_facebook_conversations_for_message_sync(max_conversations=20)
		self.assertEqual(len(selected), 20)
		empty_selected = [n for n in selected if n.startswith("E")]
		recent_selected = [n for n in selected if n.startswith("R")]
		self.assertLessEqual(len(empty_selected), 5)
		self.assertGreaterEqual(len(recent_selected), 10)

	def test_successive_runs_eventually_select_older_conversations(self):
		"""With >20 recent and >20 empty, fair quota still rotates older names."""
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(30)]
		empties = [{"name": f"E{i:02d}"} for i in range(30)]
		# Older conversations appear only in the name-ordered fair pool (not recent window).
		olds = [SimpleNamespace(name=f"Z{i:02d}") for i in range(10)]
		fair_pool = sorted(recents + olds, key=lambda r: r.name)

		cursor_box = {"value": None}

		def get_value(key, *a, **k):
			if key == self.sync.FACEBOOK_FAIR_CURSOR_KEY:
				return cursor_box["value"]
			return None

		def set_value(key, value, *a, **k):
			if key == self.sync.FACEBOOK_FAIR_CURSOR_KEY:
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
			selected = self.sync.select_facebook_conversations_for_message_sync(max_conversations=20)
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
		selected = self.sync.select_facebook_conversations_for_message_sync(max_conversations=4)
		self.assertTrue(selected)
		self.assertLessEqual(len(selected), 4)

	def test_fair_cursor_write_failure_still_returns_selection(self):
		recents = [SimpleNamespace(name=f"R{i:02d}") for i in range(10)]
		alls = recents[:]
		self.frappe.db.sql.return_value = []
		self.frappe.get_all.side_effect = lambda *a, **k: (
			recents[: k.get("limit_page_length", 10)]
			if "last_message_at" in (k.get("order_by") or "")
			else alls
		)
		self.cache.get_value.return_value = None
		self.cache.set_value.side_effect = RuntimeError("redis write failed")
		selected = self.sync.select_facebook_conversations_for_message_sync(max_conversations=4)
		self.assertTrue(selected)


class TestSyncFacebookInboxBounded(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

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
			self.sync, "sync_facebook_conversations", return_value=list_result
		), patch.object(
			self.sync,
			"select_facebook_conversations_for_message_sync",
			return_value=["NEW", "EXISTING"],
		), patch.object(
			self.sync,
			"sync_conversation_messages",
			side_effect=[
				{"ok": True, "processed": 3, "pages": 1},
				{"ok": True, "processed": 1, "pages": 1},
			],
		) as msg_sync:
			result = self.sync.sync_facebook_inbox_bounded(limit=50)

		self.assertEqual(msg_sync.call_count, 2)
		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])
		self.assertEqual(result["messages"]["synced"], 2)
		self.cache.eval.assert_called()

	def test_no_new_messages_still_ok(self):
		with patch.object(
			self.sync,
			"sync_facebook_conversations",
			return_value={"ok": True, "processed": 1, "pages": 1},
		), patch.object(
			self.sync,
			"select_facebook_conversations_for_message_sync",
			return_value=["C1"],
		), patch.object(
			self.sync,
			"sync_conversation_messages",
			return_value={"ok": True, "processed": 0, "pages": 1},
		):
			result = self.sync.sync_facebook_inbox_bounded(limit=10)
		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])

	def test_message_failures_mark_partial_not_full_success(self):
		with patch.object(
			self.sync,
			"sync_facebook_conversations",
			return_value={"ok": True, "processed": 1, "pages": 1},
		), patch.object(
			self.sync,
			"select_facebook_conversations_for_message_sync",
			return_value=["OK", "BAD"],
		), patch.object(
			self.sync,
			"sync_conversation_messages",
			side_effect=[
				{"ok": True, "processed": 2},
				{"ok": False, "error": "connector timeout"},
			],
		):
			result = self.sync.sync_facebook_inbox_bounded(limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertIn("1 conversation", result["error"])
		self.assertNotIn("Authorization", result["error"])

	def test_redacts_sensitive_failure_text(self):
		err = self.sync._safe_message_sync_error("Authorization: Bearer secret-token failed")
		self.assertEqual(err, "message sync failed")

	def test_duplicate_overlapping_run_skipped(self):
		self.cache.set.return_value = False
		with patch.object(self.sync, "sync_facebook_conversations") as list_sync:
			result = self.sync.sync_facebook_inbox_bounded(limit=10)
		list_sync.assert_not_called()
		self.assertTrue(result.get("skipped"))
		self.assertFalse(result["ok"])

	def test_passes_message_pagination_bounds(self):
		with patch.object(
			self.sync,
			"sync_facebook_conversations",
			return_value={"ok": True, "processed": 0, "pages": 1},
		) as list_sync, patch.object(
			self.sync,
			"select_facebook_conversations_for_message_sync",
			return_value=["C1"],
		), patch.object(
			self.sync,
			"sync_conversation_messages",
			return_value={"ok": True, "processed": 0},
		) as msg_sync:
			self.sync.sync_facebook_inbox_bounded(
				limit=25,
				max_conversation_pages=2,
				message_limit=10,
				max_message_pages=1,
			)
		list_sync.assert_called_once_with(limit=25, max_pages=2)
		msg_sync.assert_called_once_with("C1", limit=10, max_pages=1)


class TestScheduledAndInboxWiring(unittest.TestCase):
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
		import meta_omnichannel.jobs.sync_jobs as jobs_mod
		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(jobs_mod)
		importlib.reload(inbox_mod)
		cls.jobs = jobs_mod
		cls.inbox = inbox_mod

	def test_scheduled_uses_coordinator(self):
		payload = {"ok": True, "processed": 1, "skipped": False, "messages": {"synced": 1, "failed": []}}
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_facebook_inbox_bounded",
			return_value=payload,
		) as coord:
			self.jobs.scheduled_facebook_sync()
		coord.assert_called_once_with(limit=50)

	def test_facebook_partial_does_not_block_instagram(self):
		fb = {
			"ok": False,
			"partial": True,
			"error": "Message sync failed for 2 conversation(s)",
			"processed": 3,
		}
		ig = {"ok": True, "processed": 2}
		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		), patch.object(self.inbox, "_sync_instagram_conversations", return_value=ig):
			result = self.inbox.sync_inbox(limit=10)
		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertTrue(result["instagram"]["ok"])
		self.assertIn("Message sync failed", result["error"])

	def test_plain_js_frontend_sees_sync_inbox_error_on_message_partial(self):
		"""Production omnichannel.js shows error when sync_inbox.ok is false."""
		from pathlib import Path

		js = (
			Path(__file__).resolve().parents[1]
			/ "public"
			/ "frontend"
			/ "omnichannel.js"
		).read_text(encoding="utf-8")
		self.assertIn('if (result && result.ok === false)', js)
		self.assertIn('result.error || "Sync failed"', js)

		fb = {
			"ok": False,
			"partial": True,
			"error": "Message sync failed for 1 conversation(s)",
		}
		ig = {"ok": True, "processed": 0}
		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		), patch.object(self.inbox, "_sync_instagram_conversations", return_value=ig):
			result = self.inbox.sync_inbox(limit=10)
		# Same contract the plain-JS Sync button uses.
		self.assertTrue(result["ok"] is False)
		self.assertTrue(bool(result.get("error")))


if __name__ == "__main__":
	unittest.main()

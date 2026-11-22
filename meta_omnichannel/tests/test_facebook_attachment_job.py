"""Offline tests for Facebook attachment RQ enqueue/runner."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.throw = MagicMock(
		side_effect=lambda *a, **k: (_ for _ in ()).throw(
			frappe.ValidationError(a[0] if a else "throw")
		)
	)
	frappe.db = MagicMock()
	frappe.db.commit = MagicMock()
	frappe.db.rollback = MagicMock()
	frappe.db.exists = MagicMock(return_value=True)
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe.delete_doc = MagicMock()
	frappe.enqueue = MagicMock(return_value=SimpleNamespace(id="job-1"))
	frappe.cache = MagicMock()
	sys.modules["frappe"] = frappe

	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda v: int(v or 0)  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils

	fm = types.ModuleType("frappe.utils.file_manager")
	fm.save_file = MagicMock()  # type: ignore[attr-defined]
	fm.get_file_path = MagicMock(return_value="/tmp/private/files/secret.bin")  # type: ignore[attr-defined]
	sys.modules["frappe.utils.file_manager"] = fm

	return frappe


class TestFacebookAttachmentJob(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.media as media_mod
		import meta_omnichannel.jobs.sync_jobs as jobs_mod

		importlib.reload(media_mod)
		importlib.reload(jobs_mod)
		cls.media = media_mod
		cls.jobs = jobs_mod

	def setUp(self):
		self.frappe.db.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.exists.return_value = True
		self.frappe.db.commit = MagicMock()
		self.frappe.db.rollback = MagicMock()
		self.frappe.db.set_value.side_effect = None
		self.frappe.enqueue.reset_mock()
		self.frappe.enqueue.return_value = SimpleNamespace(id="job-1")
		self.frappe.throw.reset_mock(side_effect=True)
		self.frappe.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			self.frappe.ValidationError(a[0] if a else "throw")
		)

		self._lock_token = None
		self.cache = MagicMock()
		self.cache.make_key = MagicMock(side_effect=lambda k: f"site|{k}".encode())

		def _cache_set(key, token, **kwargs):
			self._lock_token = token
			return True

		self.cache.set = MagicMock(side_effect=_cache_set)
		self.cache.get = MagicMock(side_effect=lambda key: self._lock_token)
		def _cache_eval(script, n, *args):
			token = args[1] if len(args) > 1 else None
			if self._lock_token is None:
				return 0
			if token is not None and token != self._lock_token:
				return 0
			return 1

		self.cache.eval = MagicMock(side_effect=_cache_eval)
		self.frappe.cache = MagicMock(return_value=self.cache)

		# Default: not Ready
		self.frappe.get_all.return_value = []

	def test_deterministic_job_id(self):
		a = self.media.attachment_persist_job_id("MSG-1", 0)
		b = self.media.attachment_persist_job_id("MSG-1", 0)
		c = self.media.attachment_persist_job_id("MSG-1", 1)
		self.assertEqual(a, b)
		self.assertNotEqual(a, c)
		self.assertEqual(a, "omnichannel_fb_attach:MSG-1:0")

	def test_enqueue_uses_dedupe_long_queue(self):
		with patch.object(self.media, "is_attachment_ready", return_value=False):
			self.jobs.enqueue_facebook_attachment_persist("MSG-1", 0)
		kwargs = self.frappe.enqueue.call_args.kwargs
		self.assertEqual(
			self.frappe.enqueue.call_args.args[0],
			"meta_omnichannel.jobs.sync_jobs.run_facebook_attachment_persist",
		)
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], "omnichannel_fb_attach:MSG-1:0")
		self.assertEqual(kwargs["timeout"], self.jobs.FACEBOOK_ATTACHMENT_JOB_TIMEOUT_SEC)
		self.assertEqual(kwargs["timeout"], self.media.ATTACHMENT_LOCK_TTL_SEC)
		self.assertEqual(kwargs["message_name"], "MSG-1")
		self.assertEqual(kwargs["attachment_index"], 0)

	def test_enqueue_skips_when_ready(self):
		with patch.object(self.media, "is_attachment_ready", return_value=True):
			out = self.jobs.enqueue_facebook_attachment_persist("MSG-1", 0)
		self.assertIsNone(out)
		self.frappe.enqueue.assert_not_called()

	def test_enqueue_rejects_invalid_index(self):
		with self.assertRaises(self.frappe.ValidationError):
			self.jobs.enqueue_facebook_attachment_persist("MSG-1", True)  # type: ignore[arg-type]

	def test_run_commit_before_unlock(self):
		order: list[str] = []
		orig_release = self.media.AttachmentPersistLock.release

		def tracking_release(self_lock):
			order.append("unlock")
			return orig_release(self_lock)

		def tracking_commit():
			order.append("commit")

		self.frappe.db.commit.side_effect = tracking_commit
		result = {
			"ok": True,
			"message": "MSG-1",
			"attachment_index": 0,
			"status": "Ready",
			"file": "FILE-1",
			"content_hash": "abc",
			"error": None,
			"downloaded": True,
			"retryable": False,
		}
		with (
			patch.object(self.media.AttachmentPersistLock, "release", tracking_release),
			patch.object(
				self.media, "persist_message_attachment_unlocked", return_value=result
			) as unlocked,
		):
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertEqual(out["status"], "Ready")
		unlocked.assert_called_once()
		self.assertEqual(unlocked.call_args.kwargs.get("lock_handle") is not None, True)
		self.assertEqual(order, ["commit", "unlock"])

	def test_run_rollback_before_unlock_on_db_failure(self):
		order: list[str] = []
		orig_release = self.media.AttachmentPersistLock.release

		def tracking_release(self_lock):
			order.append("unlock")
			return orig_release(self_lock)

		def tracking_rollback():
			order.append("rollback")

		self.frappe.db.rollback.side_effect = tracking_rollback

		with (
			patch.object(self.media.AttachmentPersistLock, "release", tracking_release),
			patch.object(
				self.media,
				"persist_message_attachment_unlocked",
				side_effect=RuntimeError("db blew up"),
			),
		):
			with self.assertRaises(RuntimeError):
				self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertEqual(order, ["rollback", "unlock"])
		self.frappe.db.commit.assert_not_called()

	def test_run_failed_status_is_committed(self):
		failed = {
			"ok": False,
			"message": "MSG-1",
			"attachment_index": 0,
			"status": "Failed",
			"file": None,
			"content_hash": None,
			"error": "ConnectorError: boom",
			"downloaded": True,
			"retryable": False,
		}
		with patch.object(
			self.media, "persist_message_attachment_unlocked", return_value=failed
		):
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertEqual(out["status"], "Failed")
		self.frappe.db.commit.assert_called_once()
		self.frappe.db.rollback.assert_not_called()

	def test_run_contention_no_download_no_failed(self):
		self.cache.set.side_effect = None
		self.cache.set.return_value = False
		with patch.object(self.media, "persist_message_attachment_unlocked") as unlocked:
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertTrue(out["retryable"])
		self.assertNotEqual(out["status"], "Failed")
		unlocked.assert_not_called()
		self.frappe.db.commit.assert_not_called()

	def test_run_does_not_call_public_persist(self):
		"""No nested lock: runner must not call persist_message_attachment."""
		result = {
			"ok": True,
			"message": "MSG-1",
			"attachment_index": 0,
			"status": "Ready",
			"file": "FILE-1",
			"content_hash": "abc",
			"error": None,
			"downloaded": False,
			"retryable": False,
		}
		with (
			patch.object(self.media, "persist_message_attachment") as public,
			patch.object(
				self.media, "persist_message_attachment_unlocked", return_value=result
			),
		):
			self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		public.assert_not_called()

	def test_run_lock_released_on_every_path(self):
		self.cache.set.side_effect = None
		self.cache.set.return_value = False
		self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		# Contention: never acquired → no release eval
		self.cache.eval.assert_not_called()

		# Success path releases
		self.cache.set.side_effect = lambda key, token, **k: setattr(self, "_lock_token", token) or True
		with patch.object(
			self.media,
			"persist_message_attachment_unlocked",
			return_value={
				"ok": True,
				"message": "MSG-1",
				"attachment_index": 0,
				"status": "Ready",
				"file": "F",
				"content_hash": "h",
				"error": None,
				"downloaded": False,
				"retryable": False,
			},
		):
			self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertTrue(self.cache.eval.called)

	def test_legacy_fields_not_written_by_runner(self):
		with patch.object(
			self.media,
			"persist_message_attachment_unlocked",
			return_value={
				"ok": True,
				"message": "MSG-1",
				"attachment_index": 0,
				"status": "Ready",
				"file": "F",
				"content_hash": "h",
				"error": None,
				"downloaded": False,
				"retryable": False,
			},
		):
			self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		for c in self.frappe.db.set_value.call_args_list:
			if c.args and c.args[0] == "Omnichannel Message" and isinstance(c.args[2], dict):
				self.assertNotIn("attachment_url", c.args[2])
				self.assertNotIn("attachment_mime", c.args[2])

	def test_ownership_lost_before_commit_rolls_back(self):
		"""still_owned() False immediately before commit → rollback, no commit."""
		order: list[str] = []
		orig_release = self.media.AttachmentPersistLock.release

		def tracking_release(self_lock):
			order.append("unlock")
			return orig_release(self_lock)

		def tracking_rollback():
			order.append("rollback")

		self.frappe.db.rollback.side_effect = tracking_rollback
		result = {
			"ok": True,
			"message": "MSG-1",
			"attachment_index": 0,
			"status": "Ready",
			"file": "FILE-1",
			"content_hash": "abc",
			"error": None,
			"downloaded": True,
			"retryable": False,
			"lock_lost": False,
		}
		with (
			patch.object(self.media.AttachmentPersistLock, "release", tracking_release),
			patch.object(
				self.media.AttachmentPersistLock, "still_owned", return_value=False
			),
			patch.object(
				self.media, "persist_message_attachment_unlocked", return_value=result
			),
		):
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertTrue(out["lock_lost"])
		self.assertTrue(out["retryable"])
		self.assertFalse(out["ok"])
		self.assertNotEqual(out["status"], "Failed")
		self.assertNotEqual(out["status"], "Ready")
		self.frappe.db.commit.assert_not_called()
		self.assertEqual(order, ["rollback", "unlock"])

	def test_core_lock_lost_result_rolls_back_no_commit(self):
		order: list[str] = []
		orig_release = self.media.AttachmentPersistLock.release

		def tracking_release(self_lock):
			order.append("unlock")
			return orig_release(self_lock)

		def tracking_rollback():
			order.append("rollback")

		self.frappe.db.rollback.side_effect = tracking_rollback
		lost = self.media._lock_lost_result("MSG-1", 0, downloaded=True)
		with (
			patch.object(self.media.AttachmentPersistLock, "release", tracking_release),
			patch.object(
				self.media, "persist_message_attachment_unlocked", return_value=lost
			),
		):
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertTrue(self.media.is_lock_lost_result(out))
		self.assertTrue(out["retryable"])
		self.frappe.db.commit.assert_not_called()
		self.assertEqual(order, ["rollback", "unlock"])

	def test_ordinary_download_failure_commits_failed(self):
		failed = {
			"ok": False,
			"message": "MSG-1",
			"attachment_index": 0,
			"status": "Failed",
			"file": None,
			"content_hash": None,
			"error": "ConnectorError: boom",
			"downloaded": True,
			"retryable": False,
			"lock_lost": False,
		}
		with patch.object(
			self.media, "persist_message_attachment_unlocked", return_value=failed
		):
			out = self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.assertEqual(out["status"], "Failed")
		self.assertFalse(out.get("lock_lost"))
		self.frappe.db.commit.assert_called_once()
		self.frappe.db.rollback.assert_not_called()

	def test_db_exception_propagates_and_rolls_back(self):
		order: list[str] = []
		orig_release = self.media.AttachmentPersistLock.release

		def tracking_release(self_lock):
			order.append("unlock")
			return orig_release(self_lock)

		def tracking_rollback():
			order.append("rollback")

		self.frappe.db.rollback.side_effect = tracking_rollback
		with (
			patch.object(self.media.AttachmentPersistLock, "release", tracking_release),
			patch.object(
				self.media,
				"persist_message_attachment_unlocked",
				side_effect=RuntimeError("deadlock"),
			),
		):
			with self.assertRaises(RuntimeError):
				self.jobs.run_facebook_attachment_persist("MSG-1", 0)
		self.frappe.db.commit.assert_not_called()
		self.assertEqual(order, ["rollback", "unlock"])


if __name__ == "__main__":
	unittest.main()
"""Instagram inbound notification receiver: HMAC reuse, replay, tip-only resolve, IG job."""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from meta_omnichannel.meta_connectors.inbound_hmac import (
	SIGNATURE_HEADER,
	TIMESTAMP_HEADER,
	compute_signature_hex,
	format_signature_header,
)


SECRET = "test-inbound-hmac-secret-value"


def _job(job_id: str = "job-ig-1"):
	return SimpleNamespace(id=job_id)


def _sign(body: bytes, *, timestamp: str, secret: str = SECRET) -> dict[str, str]:
	digest = compute_signature_hex(secret=secret, timestamp=timestamp, body=body)
	return {
		TIMESTAMP_HEADER: timestamp,
		SIGNATURE_HEADER: format_signature_header(digest),
	}


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe.cache = MagicMock()
	cache = MagicMock()
	cache.get_value.return_value = None
	cache.set_value.return_value = None
	cache.make_key = lambda key, *a, **k: f"site|{key}"
	cache.set = MagicMock(return_value=True)
	cache.eval = MagicMock(return_value=1)
	frappe.cache.return_value = cache
	frappe.local = MagicMock()
	frappe.local.flags = MagicMock()
	frappe.local.response = {}
	frappe._ = lambda x: x
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	frappe.publish_realtime = MagicMock()
	frappe.clear_cache = MagicMock()
	frappe.get_request_header = MagicMock(return_value=None)
	frappe.request = MagicMock()
	frappe.request.get_data = MagicMock(return_value=b"{}")
	frappe.enqueue = MagicMock(return_value=_job("job-ig-1"))
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-19 12:00:00")  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	password_mod = types.ModuleType("frappe.utils.password")
	password_mod.get_decrypted_password = MagicMock(return_value=SECRET)  # type: ignore[attr-defined]
	password_mod.set_encrypted_password = MagicMock()  # type: ignore[attr-defined]
	rate_mod = types.ModuleType("frappe.rate_limiter")

	def _rate_limit(**_kwargs):
		def _decorator(fn):
			return fn

		return _decorator

	rate_mod.rate_limit = _rate_limit  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	sys.modules["frappe.utils.password"] = password_mod
	sys.modules["frappe.rate_limiter"] = rate_mod
	frappe.utils = utils
	return frappe


class TestInstagramInboundNotifications(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.inbound as fb_inbound
		import meta_omnichannel.facebook_messenger.sync as fb_sync
		import meta_omnichannel.instagram.api as api_mod
		import meta_omnichannel.instagram.inbound as inbound_mod
		import meta_omnichannel.instagram.sync as ig_sync
		import meta_omnichannel.jobs.sync_jobs as jobs_mod

		importlib.reload(fb_sync)
		importlib.reload(ig_sync)
		importlib.reload(fb_inbound)
		importlib.reload(jobs_mod)
		importlib.reload(inbound_mod)
		importlib.reload(api_mod)
		cls.fb_inbound = fb_inbound
		cls.inbound = inbound_mod
		cls.api = api_mod
		cls.jobs = jobs_mod

	def setUp(self):
		cache = MagicMock()
		cache.make_key = lambda key, *a, **k: f"site|{key}"
		cache.set = MagicMock(return_value=True)
		cache.eval = MagicMock(return_value=1)
		cache.get_value.return_value = None
		cache.set_value.return_value = None
		self.frappe.cache.return_value = cache
		self.cache = cache
		self.frappe.local.response = {}
		self.frappe.db.get_single_value.return_value = "tenant-1"
		self.frappe.db.get_value.reset_mock()
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = "CHAN-IG-1"
		self.frappe.get_doc.reset_mock()
		self.frappe.enqueue.reset_mock()
		self.frappe.enqueue.return_value = _job("job-ig-1")
		self.frappe.publish_realtime.reset_mock()
		self.headers: dict[str, str] = {}

		def _hdr(name):
			for k, v in self.headers.items():
				if k.lower() == (name or "").lower():
					return v
			return None

		self.frappe.get_request_header.side_effect = _hdr
		pwd = sys.modules["frappe.utils.password"]
		pwd.get_decrypted_password.return_value = SECRET

	def _payload(self, **overrides) -> dict:
		base = {
			"notification_id": "ig-notif-001",
			"tenant_id": "tenant-1",
			"provider": "instagram",
			"event_type": "message.inbound",
			"account_id": "IG_ACCT",
			"sender_id": "IGSID1",
			"preview": "hello should be ignored",
			"participant_name": "Attacker Name",
			"message_timestamp": 1_700_000_100,
			"is_echo": False,
		}
		base.update(overrides)
		return base

	def _body_and_headers(self, payload: dict, *, secret: str = SECRET, ts: str | None = None, mutate_sig=None):
		body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
		timestamp = ts or str(int(time.time()))
		headers = _sign(body, timestamp=timestamp, secret=secret)
		if mutate_sig:
			headers = mutate_sig(headers)
		return body, headers

	def _run(self, payload: dict, *, secret: str = SECRET, ts: str | None = None, mutate_sig=None):
		body, headers = self._body_and_headers(payload, secret=secret, ts=ts, mutate_sig=mutate_sig)
		self.headers = headers
		return self.inbound.process_instagram_inbound_notification(body=body)

	def test_invalid_signature_rejected(self):
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(
				self._payload(),
				mutate_sig=lambda h: {
					**h,
					SIGNATURE_HEADER: format_signature_header("f" * 64),
				},
			)
		self.assertEqual(ctx.exception.code, "signature_mismatch")
		self.assertEqual(ctx.exception.status, 401)

	def test_stale_timestamp_rejected(self):
		stale = str(int(time.time()) - 1000)
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload(), ts=stale)
		self.assertEqual(ctx.exception.code, "timestamp_stale_or_invalid")

	def test_missing_secret_returns_503(self):
		pwd = sys.modules["frappe.utils.password"]
		pwd.get_decrypted_password.return_value = None
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload())
		self.assertEqual(ctx.exception.code, "secret_not_configured")
		self.assertEqual(ctx.exception.status, 503)

	def test_wrong_tenant_rejected(self):
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload(tenant_id="other-tenant"))
		self.assertEqual(ctx.exception.code, "tenant_mismatch")
		self.assertEqual(ctx.exception.status, 403)

	def test_wrong_account_rejected(self):
		self.frappe.db.get_value.return_value = None
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload(account_id="UNKNOWN"))
		self.assertEqual(ctx.exception.code, "account_not_linked")
		self.assertEqual(ctx.exception.status, 403)

	def test_account_link_requires_connected_status(self):
		self.frappe.db.get_value.return_value = "CHAN-1"
		self.assertTrue(self.inbound.instagram_account_is_linked("IG_ACCT"))
		_args, kwargs = self.frappe.db.get_value.call_args
		filters = _args[1] if len(_args) > 1 else kwargs.get("filters")
		self.assertEqual(filters["status"], self.fb_inbound.CONNECTED_CHANNEL_STATUS)
		self.assertEqual(filters["is_enabled"], 1)
		self.assertEqual(filters["provider"], "Instagram")

	def test_echo_ignored(self):
		out = self._run(self._payload(is_echo=True))
		self.assertTrue(out["ok"])
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "echo")

	def test_facebook_provider_ignored_on_instagram_endpoint(self):
		out = self._run(self._payload(provider="facebook"))
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "unsupported_provider")

	def test_non_message_event_ignored(self):
		out = self._run(self._payload(event_type="message.delivery"))
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "unsupported_event_type")

	def test_outbound_ignored(self):
		out = self._run(self._payload(direction="outbound"))
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "outbound")

	def test_duplicate_notification_not_requeued(self):
		self.cache.set.side_effect = [True, None]
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-IG"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value=_job("job-1")
		) as enq:
			first = self._run(self._payload(notification_id="dup-ig-1"))
			second = self._run(self._payload(notification_id="dup-ig-1"))
		self.assertTrue(first["queued"])
		self.assertTrue(second.get("duplicate"))
		self.assertEqual(enq.call_count, 1)

	def test_replay_store_unavailable_fails_closed(self):
		self.cache.set.side_effect = RuntimeError("redis down")
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload())
		self.assertEqual(ctx.exception.code, "replay_store_unavailable")
		self.assertEqual(ctx.exception.status, 503)

	def test_untrusted_routing_fields_rejected(self):
		with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
			self._run(self._payload(conversation="CONV-HACK"))
		self.assertEqual(ctx.exception.code, "untrusted_routing_fields")

	def test_existing_conversation_enqueues_instagram_job(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-IG"
		), patch.object(
			self.inbound,
			"enqueue_inbound_message_sync",
			return_value=_job("job-9"),
		) as enq:
			out = self._run(self._payload())
		self.assertTrue(out["ok"])
		self.assertTrue(out["queued"])
		self.assertEqual(out["conversation"], "CONV-IG")
		self.assertEqual(out["job"], "job-9")
		enq.assert_called_once_with("CONV-IG")

	def test_enqueue_uses_instagram_job_not_facebook_job(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-IG"
		), patch.object(
			self.inbound,
			"enqueue_instagram_conversation_message_sync",
			return_value=_job("ig-job"),
		) as ig_enq, patch(
			"meta_omnichannel.jobs.sync_jobs.enqueue_conversation_message_sync"
		) as fb_enq:
			out = self._run(self._payload())
		self.assertTrue(out["queued"])
		ig_enq.assert_called_once_with("CONV-IG")
		fb_enq.assert_not_called()

	def test_page_id_alias_accepted_for_account(self):
		payload = self._payload()
		del payload["account_id"]
		payload["page_id"] = "IG_ACCT"
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-IG"
		) as resolve, patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value=_job("j1")
		):
			out = self._run(payload)
		self.assertTrue(out["queued"])
		resolve.assert_called_once_with("IG_ACCT", "IGSID1")

	def test_tip_only_existing_conversation_does_not_overwrite_metadata(self):
		def get_value(doctype, filters=None, fieldname=None, *a, **k):
			filters = filters or {}
			if doctype == "Omnichannel Conversation":
				return "CONV-EXISTING"
			if doctype == "Omnichannel Channel":
				return "CHAN-IG-1"
			return None

		self.frappe.db.get_value.side_effect = get_value
		self.frappe.get_doc.reset_mock()
		name = self.inbound.resolve_conversation_for_notification("IG_ACCT", "IGSID1")
		self.assertEqual(name, "CONV-EXISTING")
		self.frappe.get_doc.assert_not_called()

	def test_enqueue_failure_releases_replay_claim(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-IG"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", side_effect=RuntimeError("queue down")
		), patch.object(
			self.inbound, "_release_notification_id"
		) as release:
			with self.assertRaises(self.fb_inbound.InboundNotificationError) as ctx:
				self._run(self._payload(notification_id="release-me"))
		self.assertEqual(ctx.exception.code, "enqueue_failed")
		release.assert_called_once()
		self.assertEqual(release.call_args.args[0], "release-me")

	def test_api_maps_inbound_error_to_http_status(self):
		body = b'{"notification_id":"n1"}'
		self.frappe.request.get_data.return_value = body
		with patch.object(
			self.api,
			"process_instagram_inbound_notification",
			side_effect=self.fb_inbound.InboundNotificationError(
				"signature_mismatch", "Authentication failed", status=401
			),
		):
			result = self.api.notify_instagram_inbound()
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "signature_mismatch")
		self.assertEqual(self.frappe.local.response["http_status_code"], 401)

	def test_api_uses_raw_body_only(self):
		payload = self._payload()
		body, headers = self._body_and_headers(payload)
		self.headers = headers
		self.frappe.request.get_data.return_value = body
		with patch.object(
			self.api, "process_instagram_inbound_notification", return_value={"ok": True, "queued": True}
		) as proc:
			result = self.api.notify_instagram_inbound()
		self.assertTrue(result["ok"])
		_, kwargs = proc.call_args
		self.assertEqual(kwargs.get("body"), body)
		self.assertNotIn("payload", kwargs)

	def test_rate_limit_decorator_applied(self):
		self.assertEqual(self.api._INBOUND_RATE_LIMIT, 180)
		self.assertEqual(self.api._INBOUND_RATE_WINDOW_SEC, 60)


class TestInstagramInboundJobRealtime(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.inbound as fb_inbound
		import meta_omnichannel.jobs.sync_jobs as jobs_mod

		importlib.reload(fb_inbound)
		importlib.reload(jobs_mod)
		cls.jobs = jobs_mod
		cls.fb_inbound = fb_inbound

	def test_instagram_success_publishes_realtime(self):
		with patch(
			"meta_omnichannel.instagram.sync.sync_instagram_conversation_messages",
			return_value={"ok": True, "processed": 2},
		) as sync, patch.object(
			self.fb_inbound, "publish_conversation_updated"
		) as pub:
			out = self.jobs.run_instagram_conversation_message_sync("CONV-IG")
		sync.assert_called_once_with("CONV-IG")
		pub.assert_called_once()
		kwargs = pub.call_args.kwargs
		self.assertEqual(kwargs["conversation"], "CONV-IG")
		self.assertEqual(kwargs["processed"], 2)
		self.assertEqual(kwargs["source"], "message_sync")
		self.assertTrue(out["ok"])

	def test_instagram_failure_skips_realtime(self):
		with patch(
			"meta_omnichannel.instagram.sync.sync_instagram_conversation_messages",
			return_value={"ok": False, "processed": 0, "error": "down"},
		), patch.object(self.fb_inbound, "publish_conversation_updated") as pub:
			out = self.jobs.run_instagram_conversation_message_sync("CONV-IG")
		pub.assert_not_called()
		self.assertFalse(out["ok"])
		self.frappe.publish_realtime.assert_not_called()

	def test_facebook_job_still_publishes_realtime(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": True, "processed": 3},
		), patch(
			"meta_omnichannel.facebook_messenger.inbound.publish_conversation_updated"
		) as pub:
			out = self.jobs.run_conversation_message_sync("CONV-FB")
		self.assertTrue(out["ok"])
		pub.assert_called_once()
		kwargs = pub.call_args.kwargs
		self.assertEqual(kwargs["conversation"], "CONV-FB")
		self.assertEqual(kwargs["processed"], 3)

	def test_facebook_job_failure_still_skips_realtime(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": False, "error": "down"},
		), patch(
			"meta_omnichannel.facebook_messenger.inbound.publish_conversation_updated"
		) as pub:
			self.jobs.run_conversation_message_sync("CONV-FB")
		pub.assert_not_called()

	def test_publisher_provider_comes_from_instagram_conversation_doc(self):
		"""Shared publisher must label provider from trusted Conversation, not hardcode Facebook."""
		doc = MagicMock()
		doc.name = "CONV-IG"
		doc.provider = "Instagram"
		doc.assigned_to = None
		self.frappe.db.exists.return_value = True
		self.frappe.get_doc.return_value = doc
		with patch.object(
			self.fb_inbound, "_eligible_realtime_recipients", return_value=["agent@example.com"]
		):
			self.fb_inbound.publish_conversation_updated(
				conversation="CONV-IG", processed=1, source="message_sync"
			)
		self.frappe.publish_realtime.assert_called()
		args = self.frappe.publish_realtime.call_args.args
		payload = args[1] if len(args) > 1 else self.frappe.publish_realtime.call_args.kwargs.get("message")
		self.assertEqual(args[0] if args else None, self.fb_inbound.REALTIME_EVENT)
		self.assertEqual(payload["conversation"], "CONV-IG")
		self.assertEqual(payload["provider"], "Instagram")
		self.assertEqual(payload["source"], "message_sync")
		self.assertEqual(payload["processed"], 1)
		self.assertEqual(
			self.frappe.publish_realtime.call_args.kwargs.get("user"), "agent@example.com"
		)

	def test_enqueue_instagram_uses_distinct_job_id(self):
		self.frappe.enqueue.reset_mock()
		self.frappe.enqueue.return_value = _job("x")
		self.jobs.enqueue_instagram_conversation_message_sync("CONV-IG")
		kwargs = self.frappe.enqueue.call_args.kwargs
		self.assertEqual(
			kwargs["job_id"], "omnichannel_ig_msg_sync:CONV-IG"
		)
		self.assertTrue(kwargs.get("deduplicate"))
		self.assertEqual(
			self.frappe.enqueue.call_args.args[0],
			"meta_omnichannel.jobs.sync_jobs.run_instagram_conversation_message_sync",
		)

	def test_enqueue_facebook_job_id_unchanged(self):
		self.frappe.enqueue.reset_mock()
		self.frappe.enqueue.return_value = _job("x")
		self.jobs.enqueue_conversation_message_sync("CONV-FB")
		kwargs = self.frappe.enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], "omnichannel_msg_sync:CONV-FB")
		self.assertEqual(
			self.frappe.enqueue.call_args.args[0],
			"meta_omnichannel.jobs.sync_jobs.run_conversation_message_sync",
		)


class TestFacebookInboundStillIgnoresInstagram(unittest.TestCase):
	"""Regression: Facebook receiver must keep ignoring Instagram tips."""

	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.inbound as inbound_mod
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		importlib.reload(inbound_mod)
		cls.inbound = inbound_mod

	def setUp(self):
		cache = MagicMock()
		cache.make_key = lambda key, *a, **k: f"site|{key}"
		cache.set = MagicMock(return_value=True)
		self.frappe.cache.return_value = cache
		self.frappe.db.get_single_value.return_value = "tenant-1"
		self.frappe.db.get_value.return_value = "CHAN-1"
		self.headers = {}
		self.frappe.get_request_header.side_effect = lambda name: self.headers.get(name) or self.headers.get(
			(name or "").lower()
		)
		pwd = sys.modules["frappe.utils.password"]
		pwd.get_decrypted_password.return_value = SECRET

	def test_instagram_provider_ignored_on_facebook_endpoint(self):
		payload = {
			"notification_id": "n-ig",
			"tenant_id": "tenant-1",
			"provider": "instagram",
			"event_type": "message.inbound",
			"page_id": "PAGE1",
			"sender_id": "PSID1",
		}
		body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
		ts = str(int(time.time()))
		self.headers = _sign(body, timestamp=ts)
		out = self.inbound.process_facebook_inbound_notification(body=body)
		self.assertTrue(out["ok"])
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "unsupported_provider")


if __name__ == "__main__":
	unittest.main()

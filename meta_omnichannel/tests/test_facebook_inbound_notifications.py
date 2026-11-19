"""Facebook inbound notification receiver: HMAC, replay, tip-only resolve, enqueue."""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from meta_omnichannel.meta_connectors.inbound_hmac import (
	SIGNATURE_HEADER,
	TIMESTAMP_HEADER,
	compute_signature_hex,
	format_signature_header,
	signatures_match,
	timestamp_is_fresh,
	verify_request_signature,
)


SECRET = "test-inbound-hmac-secret-value"


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
	frappe.enqueue = MagicMock(return_value="job-1")
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


class TestInboundHmacPure(unittest.TestCase):
	def test_signatures_match_constant_time_equality(self):
		hex_a = "a" * 64
		self.assertTrue(signatures_match(hex_a, hex_a))
		self.assertFalse(signatures_match(hex_a, "b" * 64))
		self.assertFalse(signatures_match(hex_a, "a" * 63))

	def test_timestamp_freshness(self):
		now = 1_700_000_000
		self.assertTrue(timestamp_is_fresh(now, now=now, max_skew_sec=300))
		self.assertTrue(timestamp_is_fresh(now - 299, now=now, max_skew_sec=300))
		self.assertFalse(timestamp_is_fresh(now - 301, now=now, max_skew_sec=300))
		self.assertFalse(timestamp_is_fresh("nope", now=now))
		self.assertFalse(timestamp_is_fresh(None, now=now))

	def test_verify_rejects_bad_signature_and_stale(self):
		body = b'{"notification_id":"n1"}'
		ts = str(int(time.time()))
		ok, reason = verify_request_signature(
			secret=SECRET,
			timestamp=ts,
			signature_header=format_signature_header("0" * 64),
			body=body,
			now=int(ts),
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "signature_mismatch")

		ok, reason = verify_request_signature(
			secret=SECRET,
			timestamp=str(int(ts) - 1000),
			signature_header=format_signature_header(
				compute_signature_hex(secret=SECRET, timestamp=str(int(ts) - 1000), body=body)
			),
			body=body,
			now=int(ts),
			max_skew_sec=300,
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "timestamp_stale_or_invalid")


class TestFacebookInboundNotifications(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.inbound as inbound_mod
		import meta_omnichannel.facebook_messenger.api as api_mod
		import meta_omnichannel.jobs.sync_jobs as jobs_mod
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		importlib.reload(inbound_mod)
		importlib.reload(jobs_mod)
		importlib.reload(api_mod)
		cls.inbound = inbound_mod
		cls.api = api_mod
		cls.jobs = jobs_mod
		cls.sync = sync_mod

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
		self.frappe.db.get_value.return_value = "CHAN-FB-1"
		self.frappe.db.set_value = MagicMock()
		self.frappe.get_doc.reset_mock()
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
			"notification_id": "notif-001",
			"tenant_id": "tenant-1",
			"provider": "facebook",
			"event_type": "message.inbound",
			"page_id": "PAGE1",
			"sender_id": "PSID1",
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
		return self.inbound.process_facebook_inbound_notification(body=body)

	def test_invalid_signature_rejected(self):
		with self.assertRaises(self.inbound.InboundNotificationError) as ctx:
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
		with self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(), ts=stale)
		self.assertEqual(ctx.exception.code, "timestamp_stale_or_invalid")

	def test_wrong_tenant_rejected(self):
		with self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(tenant_id="other-tenant"))
		self.assertEqual(ctx.exception.code, "tenant_mismatch")
		self.assertEqual(ctx.exception.status, 403)

	def test_wrong_page_rejected(self):
		self.frappe.db.get_value.return_value = None
		with self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(page_id="UNKNOWN"))
		self.assertEqual(ctx.exception.code, "page_not_linked")

	def test_page_link_requires_connected_status(self):
		"""Filters must include status=Connected and is_enabled=1."""
		self.frappe.db.get_value.return_value = "CHAN-1"
		self.assertTrue(self.inbound.facebook_page_is_linked("PAGE1"))
		_args, kwargs = self.frappe.db.get_value.call_args
		filters = _args[1] if len(_args) > 1 else kwargs.get("filters")
		self.assertEqual(filters["status"], self.inbound.CONNECTED_CHANNEL_STATUS)
		self.assertEqual(filters["is_enabled"], 1)
		self.assertEqual(filters["provider"], "Facebook")

	def test_echo_ignored(self):
		out = self._run(self._payload(is_echo=True))
		self.assertTrue(out["ok"])
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "echo")

	def test_non_message_event_ignored(self):
		out = self._run(self._payload(event_type="message.delivery"))
		self.assertTrue(out["ignored"])
		self.assertEqual(out["reason"], "unsupported_event_type")

	def test_duplicate_notification_not_requeued(self):
		self.cache.set.side_effect = [True, None]
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-1"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value="job-1"
		) as enq:
			first = self._run(self._payload(notification_id="dup-1"))
			second = self._run(self._payload(notification_id="dup-1"))
		self.assertTrue(first["queued"])
		self.assertTrue(second.get("duplicate"))
		self.assertEqual(enq.call_count, 1)

	def test_existing_conversation_enqueues_sync(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-EXISTING"
		), patch.object(
			self.inbound, "enqueue_conversation_message_sync", return_value="job-9"
		) as enq:
			out = self._run(self._payload())
		self.assertTrue(out["ok"])
		self.assertTrue(out["queued"])
		self.assertEqual(out["conversation"], "CONV-EXISTING")
		enq.assert_called_once_with("CONV-EXISTING")

	def test_new_conversation_resolved_locally_without_connector_http(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-NEW"
		) as resolve, patch.object(
			self.inbound, "enqueue_conversation_message_sync", return_value="job-new"
		), patch(
			"meta_omnichannel.facebook_messenger.sync.sync_facebook_conversations"
		) as list_sync:
			out = self._run(self._payload(notification_id="new-1", sender_id="PSID-NEW"))
		self.assertEqual(out["conversation"], "CONV-NEW")
		resolve.assert_called_once_with("PAGE1", "PSID-NEW")
		list_sync.assert_not_called()

	def test_tip_does_not_update_existing_conversation_metadata(self):
		"""Existing thread: return name only — no set_value / no tip metadata."""
		self.frappe.db.get_value.return_value = "CONV-EXISTING"
		self.frappe.db.set_value = MagicMock()
		name = self.inbound.resolve_conversation_for_notification("PAGE1", "PSID1")
		self.assertEqual(name, "CONV-EXISTING")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.get_doc.assert_not_called()

	def test_new_conversation_creates_minimum_fields_only(self):
		self.frappe.db.get_value.return_value = None  # no existing conversation
		doc = MagicMock()
		doc.name = "CONV-CREATED"
		doc.insert = MagicMock()
		self.frappe.get_doc.return_value = doc
		with patch.object(self.inbound, "_ensure_facebook_channel", return_value="CHAN-1"):
			name = self.inbound.resolve_conversation_for_notification("PAGE1", "PSID-NEW")
		self.assertEqual(name, "CONV-CREATED")
		created = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(created["page_id"], "PAGE1")
		self.assertEqual(created["participant_id"], "PSID-NEW")
		self.assertEqual(created["participant_name"], "PSID-NEW")
		self.assertNotIn("last_message_preview", created)
		self.assertNotIn("last_message_at", created)
		doc.insert.assert_called_once_with(ignore_permissions=True)

	def test_untrusted_conversation_field_rejected(self):
		with self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(conversation="HACKED"))
		self.assertEqual(ctx.exception.code, "untrusted_routing_fields")

	def test_redis_unavailable_fails_closed_without_enqueue(self):
		self.cache.set.side_effect = RuntimeError("redis down")
		with patch.object(
			self.inbound, "resolve_conversation_for_notification"
		) as resolve, patch.object(
			self.inbound, "enqueue_inbound_message_sync"
		) as enq, self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(notification_id="redis-down"))
		self.assertEqual(ctx.exception.code, "replay_store_unavailable")
		self.assertEqual(ctx.exception.status, 503)
		resolve.assert_not_called()
		enq.assert_not_called()

	def test_enqueue_failure_releases_reservation_and_is_retryable(self):
		claimed_tokens: list[str] = []

		def _set(key, value, **kwargs):
			claimed_tokens.append(value)
			return True

		self.cache.set.side_effect = _set
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-1"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", side_effect=RuntimeError("queue down")
		), self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(notification_id="enq-fail"))
		self.assertEqual(ctx.exception.code, "enqueue_failed")
		self.assertEqual(ctx.exception.status, 503)
		self.cache.eval.assert_called()
		eval_args = self.cache.eval.call_args.args
		self.assertEqual(eval_args[3], claimed_tokens[0])

	def test_resolver_failure_releases_reservation(self):
		claimed_tokens: list[str] = []

		def _set(key, value, **kwargs):
			claimed_tokens.append(value)
			return True

		self.cache.set.side_effect = _set
		with patch.object(
			self.inbound,
			"resolve_conversation_for_notification",
			side_effect=self.inbound.InboundNotificationError(
				"conversation_resolve_failed", "boom", status=400
			),
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync"
		) as enq, self.assertRaises(self.inbound.InboundNotificationError) as ctx:
			self._run(self._payload(notification_id="resolve-fail"))
		self.assertEqual(ctx.exception.code, "conversation_resolve_failed")
		enq.assert_not_called()
		self.assertEqual(self.cache.eval.call_args.args[3], claimed_tokens[0])

	def test_release_is_noop_without_token(self):
		self.inbound._release_notification_id("n1", None)
		self.cache.eval.assert_not_called()

	def test_release_uses_ownership_token_compare_and_delete(self):
		self.inbound._release_notification_id("n1", "tok-abc")
		self.cache.eval.assert_called_once()
		args = self.cache.eval.call_args.args
		self.assertIn("tok-abc", args)
		self.assertIn("redis.call", args[0])

	def test_successful_enqueue_keeps_reservation(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-1"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value="job-1"
		):
			out = self._run(self._payload(notification_id="keep-1"))
		self.assertTrue(out["queued"])
		self.cache.eval.assert_not_called()

	def test_api_endpoint_maps_auth_error(self):
		body = b'{"notification_id":"x"}'
		self.frappe.request.get_data.return_value = body
		self.headers = {
			TIMESTAMP_HEADER: str(int(time.time())),
			SIGNATURE_HEADER: format_signature_header("0" * 64),
		}
		result = self.api.notify_facebook_inbound()
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "signature_mismatch")
		self.assertEqual(self.frappe.local.response.get("http_status_code"), 401)

	def test_http_path_parses_signed_raw_body_only(self):
		"""notify_facebook_inbound uses get_data body — no payload= override."""
		payload = self._payload(notification_id="http-1")
		body, headers = self._body_and_headers(payload)
		self.headers = headers
		self.frappe.request.get_data.return_value = body
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-HTTP"
		), patch.object(
			self.inbound, "enqueue_conversation_message_sync", return_value="job-http"
		):
			# process is called from api module's imported symbol — patch through api path
			with patch.object(
				self.api,
				"process_facebook_inbound_notification",
				wraps=self.inbound.process_facebook_inbound_notification,
			) as proc:
				result = self.api.notify_facebook_inbound()
		self.assertTrue(result["ok"])
		self.assertTrue(result["queued"])
		# Only body kwarg — no payload=
		_, kwargs = proc.call_args
		self.assertEqual(kwargs.get("body"), body)
		self.assertNotIn("payload", kwargs)

	def test_processor_has_no_payload_override_parameter(self):
		import inspect

		params = inspect.signature(self.inbound.process_facebook_inbound_notification).parameters
		self.assertNotIn("payload", params)

	def test_message_sync_publishes_realtime_on_success(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": True, "processed": 2},
		), patch(
			"meta_omnichannel.facebook_messenger.inbound.publish_conversation_updated"
		) as pub:
			out = self.jobs.run_conversation_message_sync("CONV-1")
		self.assertTrue(out["ok"])
		pub.assert_called_once()
		kwargs = pub.call_args.kwargs
		self.assertEqual(kwargs["conversation"], "CONV-1")
		self.assertEqual(kwargs["processed"], 2)

	def test_message_sync_skips_realtime_on_failure(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": False, "error": "down"},
		), patch(
			"meta_omnichannel.facebook_messenger.inbound.publish_conversation_updated"
		) as pub:
			self.jobs.run_conversation_message_sync("CONV-1")
		pub.assert_not_called()

	def test_rate_limit_decorator_applied_to_guest_endpoint(self):
		self.assertEqual(self.api._INBOUND_RATE_LIMIT, 180)
		self.assertEqual(self.api._INBOUND_RATE_WINDOW_SEC, 60)


if __name__ == "__main__":
	unittest.main()

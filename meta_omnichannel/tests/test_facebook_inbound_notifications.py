"""Facebook inbound notification receiver: HMAC, replay, tip-only resolve, enqueue."""

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
	signatures_match,
	timestamp_is_fresh,
	verify_request_signature,
)


SECRET = "test-inbound-hmac-secret-value"


def _job(job_id: str = "job-1"):
	"""Stand-in for an RQ Job (response must serialize ``.id``, not the object)."""
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
	frappe.enqueue = MagicMock(return_value=_job("job-1"))
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
			self.inbound, "enqueue_inbound_message_sync", return_value=_job("job-1")
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
			self.inbound, "enqueue_conversation_message_sync", return_value=_job("job-9")
		) as enq:
			out = self._run(self._payload())
		self.assertTrue(out["ok"])
		self.assertTrue(out["queued"])
		self.assertEqual(out["conversation"], "CONV-EXISTING")
		self.assertEqual(out["job"], "job-9")
		enq.assert_called_once_with("CONV-EXISTING")

	def test_job_response_serializes_id_not_rq_object(self):
		"""Regression for 921e17f: HTTP JSON must not embed a raw RQ Job."""
		rq_job = _job("rq-abc-123")
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-1"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value=rq_job
		):
			out = self._run(self._payload(notification_id="ser-1"))
		self.assertEqual(out["job"], "rq-abc-123")
		# Must be JSON-serializable (str/None), not the Job/SimpleNamespace.
		json.dumps(out)
		self.assertIsInstance(out["job"], str)

	def test_job_none_serializes_as_null(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-1"
		), patch.object(
			self.inbound, "enqueue_inbound_message_sync", return_value=None
		):
			out = self._run(self._payload(notification_id="ser-none"))
		self.assertIsNone(out["job"])
		json.dumps(out)

	def test_new_conversation_resolved_locally_without_connector_http(self):
		with patch.object(
			self.inbound, "resolve_conversation_for_notification", return_value="CONV-NEW"
		) as resolve, patch.object(
			self.inbound, "enqueue_conversation_message_sync", return_value=_job("job-new")
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
			self.inbound, "enqueue_inbound_message_sync", return_value=_job("job-1")
		):
			out = self._run(self._payload(notification_id="keep-1"))
		self.assertTrue(out["queued"])
		self.assertEqual(out["job"], "job-1")
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
			self.inbound, "enqueue_conversation_message_sync", return_value=_job("job-http")
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
		self.assertEqual(result["job"], "job-http")
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


class TestPermissionScopedRealtime(unittest.TestCase):
	"""publish_conversation_updated must target authorized users only."""

	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		# Permission helpers need get_roles / session.
		cls.frappe.session = MagicMock()
		cls.frappe.session.user = "Administrator"
		cls.frappe.get_roles = MagicMock(return_value=[])
		cls.frappe.db.exists = MagicMock(return_value=True)

		import meta_omnichannel.meta_connectors.permissions as admin_perm
		import meta_omnichannel.permissions as perm_mod
		import meta_omnichannel.facebook_messenger.inbound as inbound_mod
		import meta_omnichannel.jobs.sync_jobs as jobs_mod

		importlib.reload(admin_perm)
		importlib.reload(perm_mod)
		importlib.reload(inbound_mod)
		importlib.reload(jobs_mod)
		cls.inbound = inbound_mod
		cls.perm = perm_mod
		cls.jobs = jobs_mod

	def setUp(self):
		self.frappe.publish_realtime.reset_mock()
		self.frappe.publish_realtime.side_effect = None
		self.frappe.get_all.reset_mock()
		self.frappe.get_all.side_effect = None
		self.frappe.get_all.return_value = []
		self.frappe.get_doc.reset_mock()
		self.frappe.db.exists.reset_mock()
		self.frappe.db.exists.side_effect = None
		self.frappe.db.exists.return_value = True
		self.frappe.db.get_value.reset_mock()
		self.frappe.get_roles.reset_mock()

		# Default: Has Role returns empty; tests inject candidates via get_all.
		self.enabled: dict[str, int] = {}
		self.roles: dict[str, list[str]] = {}
		self._last_has_role_kwargs = {}

		def _get_value(doctype, name, field=None, *a, **k):
			if doctype == "User" and field == "enabled":
				return self.enabled.get(name, 0)
			return None

		def _get_roles(user=None):
			return list(self.roles.get(user or self.frappe.session.user, []))

		self.frappe.db.get_value.side_effect = _get_value
		self.frappe.get_roles.side_effect = _get_roles

	def _conv(self, *, name="CONV-1", assigned_to=None):
		doc = MagicMock()
		doc.name = name
		doc.get = MagicMock(
			side_effect=lambda key, default=None: assigned_to if key == "assigned_to" else default
		)
		# Also support attribute access used by some code paths
		doc.assigned_to = assigned_to
		self.frappe.get_doc.return_value = doc
		return doc

	def _set_role_holders(self, users_by_role: dict[str, list[str]]):
		"""Make get_all('Has Role') return plucked parents; honor limit_page_length.

		Do not assert inside the mock — exceptions are swallowed by candidate lookup.
		"""
		self._last_has_role_kwargs: dict = {}

		def _get_all(
			doctype,
			filters=None,
			fields=None,
			distinct=False,
			pluck=None,
			limit_page_length=None,
			**kwargs,
		):
			if doctype != "Has Role":
				return []
			self._last_has_role_kwargs = {
				"filters": filters,
				"distinct": distinct,
				"pluck": pluck,
				"limit_page_length": limit_page_length,
				**kwargs,
			}
			role_filter = (filters or {}).get("role")
			wanted: set[str] = set()
			if isinstance(role_filter, (list, tuple)) and len(role_filter) == 2 and role_filter[0] == "in":
				wanted = set(role_filter[1])
			elif isinstance(role_filter, str):
				wanted = {role_filter}
			out: list[str] = []
			seen: set[str] = set()
			for role, users in users_by_role.items():
				if role not in wanted:
					continue
				for u in users:
					if u in seen:
						continue
					seen.add(u)
					out.append(u)
			# Simulate get_list-style truncation if a positive page length is passed.
			if limit_page_length not in (0, None) and int(limit_page_length) > 0:
				out = out[: int(limit_page_length)]
			return out

		self.frappe.get_all.side_effect = _get_all

	def _publish_users(self) -> list[str]:
		users = []
		for call in self.frappe.publish_realtime.call_args_list:
			kwargs = call.kwargs
			args = call.args
			# event may be positional
			self.assertTrue(kwargs.get("after_commit") is True or (len(args) >= 1 and kwargs.get("after_commit")))
			self.assertIn("user", kwargs)
			self.assertTrue(kwargs["user"])
			self.assertNotEqual(kwargs.get("room"), "all")
			# Must never omit user (would fall back to site room)
			users.append(kwargs["user"])
			# Payload shape
			message = kwargs.get("message")
			if message is None and len(args) >= 2:
				message = args[1]
			if message is None and "data" in kwargs:
				# our code passes data as 2nd positional
				pass
			event = args[0] if args else kwargs.get("event")
			self.assertEqual(event, self.inbound.REALTIME_EVENT)
			payload = args[1] if len(args) > 1 else kwargs.get("message")
			self.assertEqual(payload["conversation"], "CONV-1")
			self.assertEqual(payload["provider"], "Facebook")
			self.assertIn("source", payload)
		return users

	def test_system_manager_receives_event(self):
		self._conv(assigned_to="agent@example.com")
		self._set_role_holders({"System Manager": ["sm@example.com"]})
		self.enabled = {"sm@example.com": 1, "Administrator": 0}
		self.roles = {"sm@example.com": ["System Manager"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1", processed=1)
		users = self._publish_users()
		self.assertIn("sm@example.com", users)

	def test_unassigned_agent_receives_event(self):
		self._conv(assigned_to=None)
		self._set_role_holders({"Omnichannel Agent": ["agent@example.com"]})
		self.enabled = {"agent@example.com": 1, "Administrator": 0}
		self.roles = {"agent@example.com": ["Omnichannel Agent"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.assertIn("agent@example.com", self._publish_users())

	def test_assigned_agent_receives_event(self):
		self._conv(assigned_to="owner@example.com")
		self._set_role_holders({"Omnichannel Agent": ["owner@example.com", "other@example.com"]})
		self.enabled = {"owner@example.com": 1, "other@example.com": 1, "Administrator": 0}
		self.roles = {
			"owner@example.com": ["Omnichannel Agent"],
			"other@example.com": ["Omnichannel Agent"],
		}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		users = self._publish_users()
		self.assertIn("owner@example.com", users)
		self.assertNotIn("other@example.com", users)

	def test_unrelated_agent_excluded_from_assigned_conversation(self):
		self._conv(assigned_to="owner@example.com")
		self._set_role_holders({"Omnichannel Agent": ["stranger@example.com"]})
		self.enabled = {"stranger@example.com": 1, "Administrator": 0}
		self.roles = {"stranger@example.com": ["Omnichannel Agent"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.assertEqual(self._publish_users(), [])

	def test_disabled_user_excluded(self):
		self._conv(assigned_to=None)
		self._set_role_holders({"Omnichannel Agent": ["disabled@example.com"]})
		self.enabled = {"disabled@example.com": 0, "Administrator": 0}
		self.roles = {"disabled@example.com": ["Omnichannel Agent"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.assertEqual(self.frappe.publish_realtime.call_count, 0)

	def test_user_without_omnichannel_roles_excluded(self):
		self._conv(assigned_to=None)
		# Appears in Has Role wrongly but get_roles has no omnichannel role
		self._set_role_holders({"Omnichannel Agent": ["sales@example.com"]})
		self.enabled = {"sales@example.com": 1, "Administrator": 0}
		self.roles = {"sales@example.com": ["Sales User"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.assertEqual(self.frappe.publish_realtime.call_count, 0)

	def test_enabled_administrator_receives_event(self):
		self._conv(assigned_to="someone@example.com")
		self._set_role_holders({})
		self.enabled = {"Administrator": 1}
		self.roles = {"Administrator": []}  # special-cased in user_is_omnichannel_admin
		self.inbound.publish_conversation_updated(conversation="CONV-1", processed=3)
		users = self._publish_users()
		self.assertEqual(users, ["Administrator"])
		payload = self.frappe.publish_realtime.call_args.args[1]
		self.assertEqual(payload["processed"], 3)

	def test_missing_conversation_never_broadcasts(self):
		self.frappe.db.exists.return_value = False
		self.inbound.publish_conversation_updated(conversation="MISSING")
		self.frappe.publish_realtime.assert_not_called()
		self.frappe.get_doc.assert_not_called()

	def test_every_publish_has_user_and_after_commit(self):
		self._conv(assigned_to=None)
		self._set_role_holders(
			{
				"System Manager": ["sm@example.com"],
				"Omnichannel Agent": ["agent@example.com"],
			}
		)
		self.enabled = {"sm@example.com": 1, "agent@example.com": 1, "Administrator": 1}
		self.roles = {
			"sm@example.com": ["System Manager"],
			"agent@example.com": ["Omnichannel Agent"],
			"Administrator": [],
		}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.assertGreaterEqual(self.frappe.publish_realtime.call_count, 2)
		for call in self.frappe.publish_realtime.call_args_list:
			self.assertTrue(call.kwargs.get("user"))
			self.assertTrue(call.kwargs.get("after_commit"))
			self.assertNotIn("room", call.kwargs)
			self.assertNotEqual(call.kwargs.get("room", None), "all")

	def test_no_implicit_site_wide_room(self):
		"""Calling without user would hit room 'all'; we always pass user=."""
		self._conv(assigned_to=None)
		self._set_role_holders({"Omnichannel Agent": ["agent@example.com"]})
		self.enabled = {"agent@example.com": 1, "Administrator": 0}
		self.roles = {"agent@example.com": ["Omnichannel Agent"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		for call in self.frappe.publish_realtime.call_args_list:
			self.assertIn("user", call.kwargs)
			# room must not be explicitly "all" and user must be set (avoids get_site_room)
			self.assertNotEqual(call.kwargs.get("room"), "all")

	def test_message_sync_success_still_publishes(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": True, "processed": 1},
		), patch.object(self.inbound, "publish_conversation_updated") as pub:
			# jobs module imported publish from inbound at call time
			import meta_omnichannel.jobs.sync_jobs as jobs

			importlib.reload(jobs)
			out = jobs.run_conversation_message_sync("CONV-1")
		self.assertTrue(out["ok"])
		pub.assert_called_once()

	def test_message_sync_failure_still_skips_publish(self):
		with patch(
			"meta_omnichannel.facebook_messenger.sync.sync_conversation_messages",
			return_value={"ok": False, "error": "x"},
		), patch.object(self.inbound, "publish_conversation_updated") as pub:
			import meta_omnichannel.jobs.sync_jobs as jobs

			importlib.reload(jobs)
			jobs.run_conversation_message_sync("CONV-1")
		pub.assert_not_called()

	def test_publish_exception_does_not_raise(self):
		self._conv(assigned_to=None)
		self._set_role_holders({"Omnichannel Agent": ["agent@example.com"]})
		self.enabled = {"agent@example.com": 1, "Administrator": 0}
		self.roles = {"agent@example.com": ["Omnichannel Agent"]}
		self.frappe.publish_realtime.side_effect = RuntimeError("socket down")
		# Must not raise
		self.inbound.publish_conversation_updated(conversation="CONV-1")

	def test_role_holders_beyond_default_page_size_are_all_candidates(self):
		"""get_all must use limit_page_length=0 so >20 role holders are not truncated."""
		self._conv(assigned_to=None)
		agents = [f"agent{i:02d}@example.com" for i in range(25)]
		self._set_role_holders({"Omnichannel Agent": agents})
		self.enabled = {u: 1 for u in agents}
		self.enabled["Administrator"] = 0
		self.roles = {u: ["Omnichannel Agent"] for u in agents}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		published = set(self._publish_users())
		self.assertEqual(len(published), 25)
		self.assertTrue(all(u in published for u in agents))
		# Candidate lookup used unlimited page length + distinct
		self.assertEqual(self._last_has_role_kwargs.get("limit_page_length"), 0)
		self.assertTrue(self._last_has_role_kwargs.get("distinct"))
		self.assertEqual(self._last_has_role_kwargs.get("pluck"), "parent")

	def test_one_recipient_failure_does_not_block_others(self):
		self._conv(assigned_to=None)
		self._set_role_holders(
			{"Omnichannel Agent": ["a@example.com", "b@example.com"]}
		)
		self.enabled = {"a@example.com": 1, "b@example.com": 1, "Administrator": 0}
		self.roles = {
			"a@example.com": ["Omnichannel Agent"],
			"b@example.com": ["Omnichannel Agent"],
		}

		def _pub(*args, **kwargs):
			if kwargs.get("user") == "a@example.com":
				raise RuntimeError("fail a")
			return None

		self.frappe.publish_realtime.side_effect = _pub
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		users = [c.kwargs.get("user") for c in self.frappe.publish_realtime.call_args_list]
		self.assertIn("a@example.com", users)
		self.assertIn("b@example.com", users)
		# Second recipient still attempted despite first failure
		self.assertEqual(self.frappe.publish_realtime.call_count, 2)

	def test_lookup_error_never_site_wide_broadcast(self):
		"""Has Role failure must fail closed — no publish, even if Administrator is enabled."""
		self._conv(assigned_to=None)
		self.enabled = {"Administrator": 1}
		self.roles = {"Administrator": []}
		self.frappe.get_all.side_effect = RuntimeError("has role query failed")
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.frappe.publish_realtime.assert_not_called()

	def test_conversation_exists_error_never_site_wide_broadcast(self):
		self.frappe.db.exists.side_effect = RuntimeError("db down")
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.frappe.publish_realtime.assert_not_called()

	def test_empty_recipients_never_site_wide_broadcast(self):
		self._conv(assigned_to="owner@example.com")
		self._set_role_holders({"Omnichannel Agent": ["stranger@example.com"]})
		self.enabled = {"stranger@example.com": 1, "Administrator": 0}
		self.roles = {"stranger@example.com": ["Omnichannel Agent"]}
		self.inbound.publish_conversation_updated(conversation="CONV-1")
		self.frappe.publish_realtime.assert_not_called()


if __name__ == "__main__":
	unittest.main()

"""ConnectorClient unit tests with mocked HTTP (no production connector calls)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from meta_omnichannel.meta_connectors.client import (
	ConnectorClient,
	interpret_test_response,
)
from meta_omnichannel.meta_connectors.errors import ConnectorError, ConnectorUnreachableError
from meta_omnichannel.meta_connectors.ssrf import SafeDestination


def _dest(**overrides) -> SafeDestination:
	data = dict(
		hostname="connect.tarteebagency.com",
		port=443,
		ip="1.2.3.4",
		base_path="",
		normalized_url="https://connect.tarteebagency.com",
	)
	data.update(overrides)
	return SafeDestination(**data)


class TestInterpretTestResponse(unittest.TestCase):
	def test_status_mapping(self):
		self.assertTrue(interpret_test_response(200).ok)
		self.assertEqual(interpret_test_response(401).connection_status, "Authentication Failed")
		self.assertEqual(interpret_test_response(403).connection_status, "Forbidden")
		self.assertEqual(interpret_test_response(429).connection_status, "Rate Limited")
		self.assertEqual(interpret_test_response(503).connection_status, "Server Error")
		self.assertEqual(interpret_test_response(418).connection_status, "Error")


class TestConnectorClient(unittest.TestCase):
	def setUp(self):
		self.session = MagicMock()
		self.validate_patcher = patch(
			"meta_omnichannel.meta_connectors.client.validate_connector_base_url",
			side_effect=lambda url, resolve_dns=True: url.rstrip("/"),
		)
		self.resolve_patcher = patch(
			"meta_omnichannel.meta_connectors.client.resolve_safe_destination",
			return_value=_dest(),
		)
		self.validate_patcher.start()
		self.resolve_patcher.start()
		self.client = ConnectorClient(
			base_url="https://connect.tarteebagency.com",
			api_key="secret-test-key",
			tenant_id="tenant-1",
			session=self.session,
		)

	def tearDown(self):
		self.validate_patcher.stop()
		self.resolve_patcher.stop()

	def _mock_response(self, status_code: int) -> MagicMock:
		response = MagicMock()
		response.status_code = status_code
		return response

	def test_pins_validated_ip_and_sets_host_header(self):
		self.session.request.return_value = self._mock_response(200)
		result = self.client.test_connection()
		self.assertTrue(result.ok)

		kwargs = self.session.request.call_args.kwargs
		self.assertEqual(kwargs["method"], "GET")
		self.assertEqual(kwargs["url"], "https://1.2.3.4/api/facebook/conversations")
		self.assertEqual(kwargs["params"], {"limit": 1, "offset": 0})
		self.assertFalse(kwargs["allow_redirects"])
		self.assertTrue(kwargs["verify"])
		self.assertEqual(kwargs["proxies"], {"http": None, "https": None})
		self.assertEqual(kwargs["headers"]["X-API-Key"], "secret-test-key")
		self.assertEqual(kwargs["headers"]["Host"], "connect.tarteebagency.com")
		self.assertNotIn("X-Tenant-ID", kwargs["headers"])

	def test_dns_rebinding_uses_resolved_ip_not_hostname_url(self):
		"""Even if DNS would later return a private IP, the wire URL stays pinned."""
		self.resolve_patcher.stop()
		rebinding = patch(
			"meta_omnichannel.meta_connectors.client.resolve_safe_destination",
			return_value=_dest(ip="8.8.8.8"),
		)
		rebinding.start()
		self.addCleanup(rebinding.stop)

		self.session.request.return_value = self._mock_response(200)
		self.client.test_connection()
		url = self.session.request.call_args.kwargs["url"]
		self.assertTrue(url.startswith("https://8.8.8.8/"))
		self.assertNotIn("connect.tarteebagency.com", url)
		self.assertEqual(
			self.session.request.call_args.kwargs["headers"]["Host"],
			"connect.tarteebagency.com",
		)

	def test_ipv6_pinned_url_is_bracketed(self):
		self.resolve_patcher.stop()
		v6 = patch(
			"meta_omnichannel.meta_connectors.client.resolve_safe_destination",
			return_value=_dest(ip="2001:db8::1"),
		)
		v6.start()
		self.addCleanup(v6.stop)
		self.session.request.return_value = self._mock_response(200)
		self.client.test_connection()
		self.assertTrue(
			self.session.request.call_args.kwargs["url"].startswith("https://[2001:db8::1]/")
		)

	def test_auth_failure_401(self):
		self.session.request.return_value = self._mock_response(401)
		result = self.client.test_connection()
		self.assertFalse(result.ok)
		self.assertEqual(result.connection_status, "Authentication Failed")

	def test_redirect_fail_closed(self):
		self.session.request.return_value = self._mock_response(302)
		with self.assertRaises(ConnectorError):
			self.client.test_connection()

	def test_timeout_mapped(self):
		self.session.request.side_effect = requests.Timeout()
		with self.assertRaises(ConnectorUnreachableError):
			self.client.test_connection()

	def test_error_message_does_not_include_api_key(self):
		self.session.request.return_value = self._mock_response(401)
		result = self.client.test_connection()
		self.assertNotIn("secret-test-key", result.message)

	def test_missing_api_key_rejected(self):
		with self.assertRaises(ConnectorError):
			ConnectorClient(base_url="https://connect.tarteebagency.com", api_key="")

	def test_multipart_does_not_force_json_content_type(self):
		self.session.request.return_value = self._mock_response(200)
		png = b"\x89PNG\r\n\x1a\n" + b"x" * 16
		self.client.request(
			"POST",
			"/api/facebook/messages/send-image",
			data={
				"page_id": "P1",
				"recipient_id": "S1",
				"idempotency_key": "k1",
			},
			files={"image": ("photo.png", png, "image/png")},
		)
		kwargs = self.session.request.call_args.kwargs
		self.assertEqual(kwargs["method"], "POST")
		self.assertEqual(kwargs["url"], "https://1.2.3.4/api/facebook/messages/send-image")
		self.assertEqual(kwargs["headers"]["X-API-Key"], "secret-test-key")
		self.assertNotEqual(kwargs["headers"].get("Content-Type"), "application/json")
		self.assertNotIn("json", kwargs)
		self.assertEqual(kwargs["data"]["idempotency_key"], "k1")
		self.assertIn("files", kwargs)
		name, body, mime = kwargs["files"]["image"]
		self.assertEqual(name, "photo.png")
		self.assertEqual(body, png)
		self.assertEqual(mime, "image/png")
		# Single attempt — client never retries.
		self.assertEqual(self.session.request.call_count, 1)

	def test_json_body_and_files_mutually_exclusive(self):
		with self.assertRaises(ConnectorError):
			self.client.request(
				"POST",
				"/api/facebook/messages/send-image",
				json_body={"a": 1},
				files={"image": ("a.png", b"x", "image/png")},
			)
		self.session.request.assert_not_called()

	def test_json_request_still_sets_application_json(self):
		self.session.request.return_value = self._mock_response(200)
		self.client.request(
			"POST",
			"/api/facebook/messages/send",
			json_body={"page_id": "P1", "text": "hi", "recipient_id": "S1", "idempotency_key": "k"},
		)
		kwargs = self.session.request.call_args.kwargs
		self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
		self.assertEqual(kwargs["json"]["text"], "hi")
		self.assertNotIn("files", kwargs)


if __name__ == "__main__":
	unittest.main()

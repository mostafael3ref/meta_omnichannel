"""Pinned HTTPS transport tests (offline — no production connector)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.meta_connectors.pinned_http import (
	HostnameTLSAdapter,
	assert_tls_verify_enabled,
	build_pinned_request_url,
	create_pinned_session,
	pinned_request_headers,
)
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


class TestPinnedHTTP(unittest.TestCase):
	def test_build_pinned_ipv4_and_ipv6_urls(self):
		self.assertEqual(
			build_pinned_request_url(_dest(), "/api/facebook/conversations"),
			"https://1.2.3.4/api/facebook/conversations",
		)
		self.assertEqual(
			build_pinned_request_url(_dest(ip="2001:db8::2"), "/api/x"),
			"https://[2001:db8::2]/api/x",
		)
		self.assertEqual(
			build_pinned_request_url(_dest(port=8443), "/api/x"),
			"https://1.2.3.4:8443/api/x",
		)

	def test_host_header_uses_hostname_not_ip(self):
		headers = pinned_request_headers(_dest(), {"Accept": "application/json"})
		self.assertEqual(headers["Host"], "connect.tarteebagency.com")
		headers = pinned_request_headers(_dest(port=8443), {})
		self.assertEqual(headers["Host"], "connect.tarteebagency.com:8443")

	def test_tls_verify_cannot_be_disabled(self):
		with self.assertRaises(ConnectorError):
			assert_tls_verify_enabled({"verify": False})
		assert_tls_verify_enabled({"verify": True})

	def test_session_disables_trust_env_and_proxies(self):
		session = create_pinned_session("connect.tarteebagency.com")
		self.assertFalse(session.trust_env)
		self.assertEqual(session.proxies.get("http"), None)
		self.assertEqual(session.proxies.get("https"), None)
		adapter = session.get_adapter("https://connect.tarteebagency.com/")
		self.assertIsInstance(adapter, HostnameTLSAdapter)
		self.assertEqual(adapter.server_hostname, "connect.tarteebagency.com")
		session.close()

	def test_adapter_sets_assert_hostname_and_server_hostname(self):
		adapter = HostnameTLSAdapter(server_hostname="connect.tarteebagency.com")
		captured = {}

		class FakePoolManager:
			def __init__(self, **kwargs):
				captured.update(kwargs)

		with patch("meta_omnichannel.meta_connectors.pinned_http.PoolManager", FakePoolManager):
			adapter.init_poolmanager(1, 1, block=False)

		self.assertEqual(captured["assert_hostname"], "connect.tarteebagency.com")
		self.assertEqual(captured["server_hostname"], "connect.tarteebagency.com")

	def test_proxy_manager_refused(self):
		adapter = HostnameTLSAdapter(server_hostname="connect.tarteebagency.com")
		with self.assertRaises(ConnectorError):
			adapter.proxy_manager_for("http://proxy.local:8080")

	def test_env_proxy_ignored_by_connector_client_kwargs(self):
		"""ConnectorClient always passes proxies=None map regardless of environ."""
		from meta_omnichannel.meta_connectors.client import ConnectorClient

		session = MagicMock()
		session.request.return_value = MagicMock(status_code=200)
		with patch(
			"meta_omnichannel.meta_connectors.client.validate_connector_base_url",
			side_effect=lambda url, resolve_dns=True: url.rstrip("/"),
		), patch(
			"meta_omnichannel.meta_connectors.client.resolve_safe_destination",
			return_value=_dest(),
		), patch.dict("os.environ", {"HTTPS_PROXY": "http://evil-proxy:8080"}, clear=False):
			client = ConnectorClient(
				base_url="https://connect.tarteebagency.com",
				api_key="k",
				session=session,
			)
			client.test_connection()
			kwargs = session.request.call_args.kwargs
			self.assertEqual(kwargs["proxies"], {"http": None, "https": None})
			self.assertFalse(kwargs["allow_redirects"])
			self.assertTrue(kwargs["verify"])


if __name__ == "__main__":
	unittest.main()

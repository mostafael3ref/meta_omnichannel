"""SSRF / URL validation unit tests (no live network to the production connector)."""

from __future__ import annotations

import ipaddress
import socket
import unittest
from unittest.mock import patch

from meta_omnichannel.meta_connectors.errors import ConnectorURLError
from meta_omnichannel.meta_connectors.ssrf import (
	format_ip_for_url,
	is_blocked_ip,
	resolve_safe_destination,
	validate_connector_base_url,
)


class TestIsBlockedIP(unittest.TestCase):
	def test_loopback_and_private(self):
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("127.0.0.1")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("10.0.0.5")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("192.168.1.10")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("172.16.5.1")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("169.254.169.254")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("::1")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("fc00::1")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("fe80::1")))
		self.assertTrue(is_blocked_ip(ipaddress.ip_address("224.0.0.1")))

	def test_public_ip_allowed(self):
		self.assertFalse(is_blocked_ip(ipaddress.ip_address("1.1.1.1")))
		self.assertFalse(is_blocked_ip(ipaddress.ip_address("8.8.8.8")))
		self.assertFalse(is_blocked_ip(ipaddress.ip_address("2001:4860:4860::8888")))


class TestFormatIPForURL(unittest.TestCase):
	def test_ipv4_and_ipv6(self):
		self.assertEqual(format_ip_for_url("1.2.3.4"), "1.2.3.4")
		self.assertEqual(format_ip_for_url("2001:db8::1"), "[2001:db8::1]")


class TestValidateConnectorBaseURL(unittest.TestCase):
	def setUp(self):
		self.allowlist = patch(
			"meta_omnichannel.meta_connectors.ssrf.ALLOWED_CONNECTOR_HOSTS",
			frozenset({"connect.tarteebagency.com", "connect.example.com"}),
		)
		self.allowlist.start()

	def tearDown(self):
		self.allowlist.stop()

	def test_requires_https(self):
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("http://connect.example.com", resolve_dns=False)

	def test_rejects_credentials_and_query(self):
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://user:pass@connect.example.com", resolve_dns=False)
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://connect.example.com?x=1", resolve_dns=False)

	def test_rejects_localhost_names(self):
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://localhost", resolve_dns=False)
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://metadata.google.internal", resolve_dns=False)

	def test_rejects_literal_ip_urls(self):
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://127.0.0.1", resolve_dns=False)
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://1.1.1.1", resolve_dns=False)

	def test_rejects_non_allowlisted_host(self):
		with self.assertRaises(ConnectorURLError):
			validate_connector_base_url("https://evil.example.com", resolve_dns=False)

	def test_normalizes_trailing_slash(self):
		url = validate_connector_base_url("https://connect.example.com/", resolve_dns=False)
		self.assertEqual(url, "https://connect.example.com")

	def test_dns_blocked_resolution_fail_closed(self):
		fake = [(socket.AF_INET, None, None, None, ("10.0.0.8", 0))]
		with patch("meta_omnichannel.meta_connectors.ssrf.socket.getaddrinfo", return_value=fake):
			with self.assertRaises(ConnectorURLError):
				validate_connector_base_url("https://connect.example.com", resolve_dns=True)

	def test_dns_public_resolution_ok(self):
		fake = [(socket.AF_INET, None, None, None, ("1.1.1.1", 0))]
		with patch("meta_omnichannel.meta_connectors.ssrf.socket.getaddrinfo", return_value=fake):
			url = validate_connector_base_url("https://connect.example.com", resolve_dns=True)
			self.assertEqual(url, "https://connect.example.com")

	def test_resolve_safe_destination_pins_validated_ip(self):
		fake = [(socket.AF_INET, None, None, None, ("1.2.3.4", 0))]
		with patch("meta_omnichannel.meta_connectors.ssrf.socket.getaddrinfo", return_value=fake):
			dest = resolve_safe_destination("https://connect.example.com")
			self.assertEqual(dest.ip, "1.2.3.4")
			self.assertEqual(dest.hostname, "connect.example.com")
			self.assertEqual(dest.host_header, "connect.example.com")

	def test_resolve_rejects_if_any_address_is_blocked(self):
		fake = [
			(socket.AF_INET, None, None, None, ("1.2.3.4", 0)),
			(socket.AF_INET, None, None, None, ("169.254.169.254", 0)),
		]
		with patch("meta_omnichannel.meta_connectors.ssrf.socket.getaddrinfo", return_value=fake):
			with self.assertRaises(ConnectorURLError):
				resolve_safe_destination("https://connect.example.com")

	def test_resolve_ipv6_public(self):
		fake = [(socket.AF_INET6, None, None, None, ("2001:4860:4860::8888", 0, 0, 0))]
		with patch("meta_omnichannel.meta_connectors.ssrf.socket.getaddrinfo", return_value=fake):
			dest = resolve_safe_destination("https://connect.example.com")
			self.assertEqual(dest.ip, "2001:4860:4860::8888")


if __name__ == "__main__":
	unittest.main()

"""Whitelisted API tests with frappe mocked (no production connector / Bench)."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_fake_frappe() -> MagicMock:
	"""Minimal frappe stub so api.connector can be imported offline."""
	frappe = MagicMock()
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.ValidationError = type("ValidationError", (Exception,), {})

	def throw(msg, exc=None):
		exc_type = exc or Exception
		raise exc_type(msg)

	frappe.throw.side_effect = throw
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	frappe._ = lambda x: x
	frappe.session.user = "Administrator"
	frappe.db = MagicMock()
	frappe.clear_cache = MagicMock()
	frappe.logger = MagicMock(return_value=MagicMock())
	frappe.has_permission = MagicMock(return_value=True)
	frappe.get_roles = MagicMock(return_value=["System Manager"])
	frappe.utils = types.ModuleType("frappe.utils")
	frappe.utils.now_datetime = MagicMock(return_value="2026-04-12 12:00:00")
	password_mod = types.ModuleType("frappe.utils.password")
	password_mod.get_decrypted_password = MagicMock(return_value="test-api-key")
	frappe.utils.password = password_mod

	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = frappe.utils
	sys.modules["frappe.utils.password"] = password_mod
	return frappe


class TestTestConnectorConnection(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()

	def setUp(self):
		# Fresh import each test after frappe stub exists
		import importlib

		import meta_omnichannel.api.connector as connector_api
		import meta_omnichannel.meta_connectors.permissions as permissions

		importlib.reload(permissions)
		importlib.reload(connector_api)
		self.api = connector_api
		self.frappe.has_permission.return_value = True
		self.frappe.session.user = "Administrator"
		self.frappe.get_roles.return_value = ["System Manager"]

		settings = MagicMock()
		settings.connector_base_url = "https://connect.example.com"
		settings.connector_tenant_id = "tenant-1"
		self.frappe.get_single.return_value = settings

	def test_success_updates_status(self):
		from meta_omnichannel.meta_connectors.client import ConnectorTestResult

		fake_result = ConnectorTestResult(
			ok=True,
			status_code=200,
			connection_status="Connected",
			message="ok",
		)
		with patch.object(self.api, "ConnectorClient") as client_cls:
			client_cls.return_value.test_connection.return_value = fake_result
			with patch.object(self.api, "_get_api_key", return_value="test-api-key"):
				out = self.api.test_connector_connection()

		self.assertTrue(out["ok"])
		self.assertEqual(out["connection_status"], "Connected")
		self.assertNotIn("api_key", out)
		self.assertNotIn("test-api-key", str(out))
		args = self.frappe.db.set_value.call_args
		self.assertEqual(args.args[0], "Omnichannel Settings")
		self.assertEqual(args.args[2]["connection_status"], "Connected")

	def test_rejects_non_admin(self):
		self.frappe.session.user = "user@example.com"
		self.frappe.get_roles.return_value = ["Sales User"]
		with self.assertRaises(self.frappe.PermissionError):
			self.api.test_connector_connection()

	def test_response_never_contains_api_key_on_failure(self):
		from meta_omnichannel.meta_connectors.errors import ConnectorAuthError

		with patch.object(self.api, "_get_api_key", return_value="super-secret-key"):
			with patch.object(self.api, "ConnectorClient") as client_cls:
				client_cls.return_value.test_connection.side_effect = ConnectorAuthError(
					"Invalid or missing API key (HTTP 401)."
				)
				out = self.api.test_connector_connection()

		self.assertFalse(out["ok"])
		self.assertNotIn("super-secret-key", str(out))
		self.assertNotIn("super-secret-key", str(self.frappe.db.set_value.call_args))


if __name__ == "__main__":
	unittest.main()

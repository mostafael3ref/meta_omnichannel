"""Provider registry status tests."""

from __future__ import annotations

import unittest

from meta_omnichannel.providers import get_provider_statuses
from meta_omnichannel.providers.base import ProviderCapability


class TestProviders(unittest.TestCase):
	def test_facebook_fully_enabled(self):
		statuses = {row["provider"]: row for row in get_provider_statuses()}
		self.assertTrue(statuses["Facebook"]["external_enabled"])
		self.assertEqual(statuses["Facebook"]["missing_contracts"], [])

	def test_instagram_list_only_enabled(self):
		statuses = {row["provider"]: row for row in get_provider_statuses()}
		ig = statuses["Instagram"]
		self.assertTrue(ig["external_enabled"])
		self.assertEqual(
			ig["capabilities"][ProviderCapability.LIST_CONVERSATIONS],
			"/api/instagram/conversations",
		)
		self.assertIsNone(ig["capabilities"][ProviderCapability.GET_MESSAGES])
		self.assertIsNone(ig["capabilities"][ProviderCapability.SEND_TEXT])
		self.assertIn(ProviderCapability.GET_MESSAGES, ig["missing_contracts"])
		self.assertIn(ProviderCapability.SEND_TEXT, ig["missing_contracts"])

	def test_whatsapp_still_disabled(self):
		statuses = {row["provider"]: row for row in get_provider_statuses()}
		self.assertFalse(statuses["WhatsApp"]["external_enabled"])
		self.assertTrue(statuses["WhatsApp"]["missing_contracts"])


if __name__ == "__main__":
	unittest.main()

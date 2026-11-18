"""Pure helpers for Facebook sync mapping."""

from __future__ import annotations

import unittest

from meta_omnichannel.utils.threading_keys import facebook_thread_key


class TestSyncHelpers(unittest.TestCase):
	def test_thread_key(self):
		self.assertEqual(facebook_thread_key("page", "user"), "page:user")


if __name__ == "__main__":
	unittest.main()

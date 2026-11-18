"""Tests for connector datetime parsing (no Frappe required)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from meta_omnichannel.utils.datetimes import parse_connector_datetime


class TestParseConnectorDatetime(unittest.TestCase):
	def test_none_and_empty(self):
		self.assertIsNone(parse_connector_datetime(None))
		self.assertIsNone(parse_connector_datetime(""))
		self.assertIsNone(parse_connector_datetime("   "))

	def test_iso_string(self):
		dt = parse_connector_datetime("2026-11-18T10:30:00Z")
		self.assertEqual(dt, datetime(2026, 11, 18, 10, 30, 0))

	def test_unix_seconds(self):
		dt = parse_connector_datetime(1_700_000_000)
		self.assertEqual(dt, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).replace(tzinfo=None))

	def test_unix_milliseconds(self):
		dt = parse_connector_datetime(1_700_000_000_000)
		self.assertEqual(dt, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).replace(tzinfo=None))

	def test_invalid_string(self):
		self.assertIsNone(parse_connector_datetime("not-a-date"))

	def test_bool_rejected(self):
		self.assertIsNone(parse_connector_datetime(True))
		self.assertIsNone(parse_connector_datetime(False))

	def test_datetime_passthrough(self):
		raw = datetime(2026, 1, 2, 3, 4, 5)
		self.assertEqual(parse_connector_datetime(raw), raw)


if __name__ == "__main__":
	unittest.main()
